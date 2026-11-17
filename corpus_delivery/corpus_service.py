# -*- coding: utf-8 -*-
"""
corpus_service.py — 分类语料一致交付后端（纯 Python 标准库 + SQLite，从零实现）

职责
====
1. 一次性初始化非空有限标签集合（此后不可变）。
2. 样本登记：唯一非空字符串标识 + 不可修改的字符串正文 + train/eval 归属（可修订）。
   不同标识的同文样本各自独立保留。
3. 重复关系：已登记不同样本间的无向边，可增删；反向视为同边；
   重复添加 / 删除不存在的边一律拒绝。按传递连通归组，孤立样本自成一组。
4. 双人标注：每个样本最多两名不同标注者各持一个当前标签；同一人员重复提交替换其
   当前标注；第三人提交拒绝；每次成功提交生成全局不复用的修订身份（即使标签未变）。
5. 组标签确认：
   - 组内每个样本恰有两人标注且全部标签一致 → 自动确认；
   - 标注齐全但不一致 → 只能由人工仲裁指定集合内标签（缺少双人标注不得仲裁豁免）；
   - 仲裁提交须引用完整组成员及各人的当前标注修订，引用与当前状态不符则拒绝，
     成功则替换该组当前仲裁；使用仲裁时重新核对完整成员及修订，仅完全匹配才有效；
     归属（split）变化本身不使仲裁失效。
6. 交付：指定无重复样本集合（空集合合法）；须完整纳入每个涉及的重复组，组内归属
   一致且标签已确认；失败返回实际阻塞原因且不发布部分结果；成功返回唯一交付标识
   及完整快照（原样本、最终标签、归属、完整组成员、标注修订，依赖仲裁时附仲裁内容）。
   核验与保存基于同一已提交状态（单事务），并发修订与交付串行等价；成功交付不可修改，
   可按标识完整重读，不受后来任何修订影响；重复交付调用重新核验，成功则产生独立交付。
7. 持久化：已确认修订与交付持久保留，重启不丢失、不暴露半次提交（SQLite 事务 +
   WAL + synchronous=FULL）。

接口与错误格式
==============
公开方法均为 ``CorpusService`` 的同步方法，参数与返回值均为 JSON 可序列化的
Python 对象（UTF-8 字符串）。统一返回信封：

    成功: {"ok": True,  "data": <方法特定数据>}
    失败: {"ok": False, "error": {"code": <机器可读码>, "message": <人类可读原因>,
                                   "details": <结构化补充>}}

错误码：
    INVALID_INPUT     非法输入（空标识、未知标签、自环边、交付集合含重复 id 等）
    UNKNOWN_REFERENCE 未知引用（不存在的样本、交付标识等）
    CONFLICT          业务冲突（重复登记、第三人标注、边已存在/不存在、组未完整纳入、
                      组内归属不一致、标签未确认、仲裁证据失效等）
    STATE             状态错误（未初始化 / 重复初始化标签集合）
    INTERNAL          非预期内部错误（不修改业务状态）

任何失败调用都不改变持久化状态；一次调用内多处错误时，任选其一真实原因返回。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA_VERSION = "1"
SPLITS = ("train", "eval")
SEQ_NAMES = ("sample", "revision", "arbitration", "delivery")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seq (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    sample_id  TEXT PRIMARY KEY,
    text       TEXT NOT NULL,               -- 正文：登记后不可修改（无更新接口）
    split      TEXT NOT NULL CHECK (split IN ('train','eval')),
    created_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (           -- 重复关系无向边，a < b 规范化存储
    a TEXT NOT NULL REFERENCES samples(sample_id),
    b TEXT NOT NULL REFERENCES samples(sample_id),
    PRIMARY KEY (a, b),
    CHECK (a < b)
);
CREATE TABLE IF NOT EXISTS annotations (     -- 每个 (样本, 标注者) 的当前标注
    sample_id   TEXT NOT NULL REFERENCES samples(sample_id),
    annotator   TEXT NOT NULL,
    label       TEXT NOT NULL,
    revision_id TEXT NOT NULL UNIQUE,
    seq         INTEGER NOT NULL UNIQUE,
    PRIMARY KEY (sample_id, annotator)
);
CREATE TABLE IF NOT EXISTS annotation_revisions (  -- 全部历史修订（不可变，仅追加）
    revision_id TEXT PRIMARY KEY,
    sample_id   TEXT NOT NULL REFERENCES samples(sample_id),
    annotator   TEXT NOT NULL,
    label       TEXT NOT NULL,
    seq         INTEGER NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS arbitrations (    -- 仲裁记录（不可变，仅追加；按成员集取最新为当前）
    arb_id     TEXT PRIMARY KEY,
    member_key TEXT NOT NULL,                -- 规范化成员集：JSON(升序样本 id 列表)
    evidence   TEXT NOT NULL,                -- JSON: {sample_id: {annotator: revision_id}}
    label      TEXT NOT NULL,
    seq        INTEGER NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_arbitrations_member ON arbitrations(member_key, seq);
CREATE TABLE IF NOT EXISTS deliveries (      -- 成功交付快照（不可变，仅追加）
    delivery_id TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,               -- JSON 完整快照
    seq         INTEGER NOT NULL UNIQUE
);
"""


class CorpusError(Exception):
    """业务错误：非法输入 / 未知引用 / 业务冲突 / 状态错误。抛出时不改变任何状态。"""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _require_non_empty_str(value, field):
    if not isinstance(value, str) or value == "":
        raise CorpusError("INVALID_INPUT",
                          "%s must be a non-empty string" % field,
                          {"field": field})
    return value


def _validate_label_set(labels):
    if not isinstance(labels, (list, tuple)) or len(labels) == 0:
        raise CorpusError(
            "INVALID_INPUT",
            "label set must be a non-empty finite list of distinct non-empty strings")
    clean, seen = [], set()
    for lb in labels:
        if not isinstance(lb, str) or lb == "":
            raise CorpusError("INVALID_INPUT",
                              "every label must be a non-empty string",
                              {"label": repr(lb)})
        if lb in seen:
            raise CorpusError("INVALID_INPUT",
                              "duplicate label in label set",
                              {"label": lb})
        seen.add(lb)
        clean.append(lb)
    return clean


class CorpusService:
    """语料交付后端服务。所有公开方法返回统一信封 dict，线程安全，可多进程共用同一库文件。"""

    def __init__(self, db_path):
        self._db_path = str(db_path)
        self._lock = threading.RLock()   # 进程内串行化；跨进程由 SQLite BEGIN IMMEDIATE 串行化
        self._init_schema()

    # ------------------------------------------------------------------ 基础设施

    def _connect(self):
        conn = sqlite3.connect(self._db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_schema(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = FULL")
                conn.executescript(SCHEMA_SQL)   # 建表语句均为 IF NOT EXISTS，可重入
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for name in SEQ_NAMES:
                        conn.execute("INSERT OR IGNORE INTO seq(name, value) VALUES (?, 0)", (name,))
                    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                                 (SCHEMA_VERSION,))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

    @contextmanager
    def _tx(self):
        """单事务执行检查+写入：BEGIN IMMEDIATE 保证核验与保存基于同一已提交状态，
        并发修订与交付在该事务边界上串行等价；异常整体回滚，不暴露半次提交。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            conn.close()

    def _run(self, fn, *args):
        """公开方法包装：串行化 + 单事务 + 统一信封。"""
        with self._lock:
            try:
                with self._tx() as conn:
                    data = fn(conn, *args)
                return {"ok": True, "data": data}
            except CorpusError as e:
                return {"ok": False, "error": {"code": e.code,
                                               "message": e.message,
                                               "details": e.details}}
            except Exception as e:  # 非预期错误：统一 INTERNAL，不泄漏内部细节，状态已回滚
                return {"ok": False, "error": {"code": "INTERNAL",
                                               "message": "%s: %s" % (type(e).__name__, e),
                                               "details": {}}}

    def _next_seq(self, conn, name):
        """单调递增序号（同事务内分配，回滚则不消耗）→ 生成不复用身份。"""
        conn.execute("UPDATE seq SET value = value + 1 WHERE name = ?", (name,))
        row = conn.execute("SELECT value FROM seq WHERE name = ?", (name,)).fetchone()
        return row["value"]

    def _get_labels(self, conn):
        row = conn.execute("SELECT value FROM meta WHERE key = 'labels'").fetchone()
        return None if row is None else json.loads(row["value"])

    def _require_labels(self, conn):
        labels = self._get_labels(conn)
        if labels is None:
            raise CorpusError("STATE",
                              "label set is not initialized; call initialize_labels first")
        return labels

    # ------------------------------------------------------------------ 只读派生状态

    def _component(self, conn, sample_id):
        """包含 sample_id 的传递连通组（升序列表）；孤立样本自成一组。"""
        adj = {}
        for row in conn.execute("SELECT a, b FROM edges"):
            adj.setdefault(row["a"], []).append(row["b"])
            adj.setdefault(row["b"], []).append(row["a"])
        seen, stack = {sample_id}, [sample_id]
        while stack:
            x = stack.pop()
            for y in adj.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return sorted(seen)

    def _sample_rows(self, conn, sample_ids):
        if not sample_ids:
            return {}
        marks = ",".join("?" * len(sample_ids))
        rows = conn.execute(
            "SELECT sample_id, text, split FROM samples WHERE sample_id IN (%s)" % marks,
            list(sample_ids))
        return {r["sample_id"]: {"text": r["text"], "split": r["split"]} for r in rows}

    def _annotations_map(self, conn, sample_ids):
        """{sample_id: {annotator: {"revision_id":..., "label":...}}}，按键序确定。"""
        result = {sid: {} for sid in sample_ids}
        if not sample_ids:
            return result
        marks = ",".join("?" * len(sample_ids))
        rows = conn.execute(
            "SELECT sample_id, annotator, label, revision_id FROM annotations "
            "WHERE sample_id IN (%s) ORDER BY sample_id, annotator" % marks,
            list(sample_ids))
        for r in rows:
            result[r["sample_id"]][r["annotator"]] = {
                "revision_id": r["revision_id"], "label": r["label"]}
        return result

    @staticmethod
    def _evidence_of(ann_map, members):
        return {m: {a: info["revision_id"] for a, info in ann_map[m].items()}
                for m in members}

    def _current_arbitration(self, conn, members):
        """该成员集当前（最新一次成功提交的）仲裁；不存在则为 None。"""
        key = json.dumps(sorted(members), ensure_ascii=False)
        row = conn.execute(
            "SELECT arb_id, label, evidence, seq FROM arbitrations "
            "WHERE member_key = ? ORDER BY seq DESC LIMIT 1", (key,)).fetchone()
        if row is None:
            return None
        return {"arb_id": row["arb_id"], "label": row["label"],
                "members": sorted(members), "evidence": json.loads(row["evidence"])}

    def _group_confirmation(self, conn, members):
        """组标签确认。返回 {"status": "auto"|"arbitration", ...}；未确认则抛出
        携带实际阻塞原因的 CorpusError。"""
        ann = self._annotations_map(conn, members)
        incomplete = [m for m in members if len(ann[m]) != 2]
        if incomplete:
            raise CorpusError(
                "CONFLICT",
                "group label is not confirmed: every sample needs exactly two "
                "annotations (missing dual annotation cannot be exempted, even "
                "by arbitration)",
                {"group": list(members),
                 "samples_lacking_dual_annotation": incomplete})
        labels = sorted({info["label"] for m in members for info in ann[m].values()})
        if len(labels) == 1:
            return {"status": "auto", "label": labels[0], "annotations": ann}
        arb = self._current_arbitration(conn, members)
        if arb is None:
            raise CorpusError(
                "CONFLICT",
                "annotations are complete but inconsistent and no current "
                "arbitration exists for this group",
                {"group": list(members), "labels": labels})
        current_evidence = self._evidence_of(ann, members)
        if arb["evidence"] != current_evidence:
            raise CorpusError(
                "CONFLICT",
                "the group's current arbitration evidence does not match current "
                "annotation revisions (evidence invalidated by later re-annotation "
                "or membership change)",
                {"group": list(members), "arb_id": arb["arb_id"],
                 "expected_evidence": current_evidence,
                 "arbitration_evidence": arb["evidence"]})
        return {"status": "arbitration", "label": arb["label"],
                "annotations": ann, "arbitration": arb}

    # ------------------------------------------------------------------ 公开 API

    def initialize_labels(self, labels):
        """一次性初始化非空有限标签集合。"""
        return self._run(self._initialize_labels, labels)

    def register_sample(self, sample_id, text, split):
        return self._run(self._register_sample, sample_id, text, split)

    def reassign_split(self, sample_id, split):
        return self._run(self._reassign_split, sample_id, split)

    def add_duplicate(self, sample_id_a, sample_id_b):
        return self._run(self._add_duplicate, sample_id_a, sample_id_b)

    def remove_duplicate(self, sample_id_a, sample_id_b):
        return self._run(self._remove_duplicate, sample_id_a, sample_id_b)

    def submit_annotation(self, sample_id, annotator, label):
        return self._run(self._submit_annotation, sample_id, annotator, label)

    def submit_arbitration(self, members, evidence, label):
        return self._run(self._submit_arbitration, members, evidence, label)

    def deliver(self, sample_ids):
        return self._run(self._deliver, sample_ids)

    def get_delivery(self, delivery_id):
        return self._run(self._get_delivery, delivery_id)

    def get_sample(self, sample_id):
        return self._run(self._get_sample, sample_id)

    def describe_group(self, sample_id):
        return self._run(self._describe_group, sample_id)

    def status(self):
        return self._run(self._status)

    # ------------------------------------------------------------------ API 实现

    def _initialize_labels(self, conn, labels):
        if self._get_labels(conn) is not None:
            raise CorpusError("STATE",
                              "label set has already been initialized and is immutable")
        clean = _validate_label_set(labels)
        conn.execute("INSERT INTO meta(key, value) VALUES ('labels', ?)",
                     (json.dumps(clean, ensure_ascii=False),))
        return {"labels": clean}

    def _register_sample(self, conn, sample_id, text, split):
        self._require_labels(conn)
        _require_non_empty_str(sample_id, "sample_id")
        if not isinstance(text, str):
            raise CorpusError("INVALID_INPUT",
                              "text must be a string (immutable once registered)",
                              {"field": "text"})
        if split not in SPLITS:
            raise CorpusError("INVALID_INPUT",
                              "split must be one of %s" % (list(SPLITS),),
                              {"split": split})
        exists = conn.execute("SELECT 1 FROM samples WHERE sample_id = ?",
                              (sample_id,)).fetchone()
        if exists:
            raise CorpusError("CONFLICT",
                              "sample is already registered",
                              {"sample_id": sample_id})
        seq = self._next_seq(conn, "sample")
        conn.execute(
            "INSERT INTO samples(sample_id, text, split, created_seq) VALUES (?,?,?,?)",
            (sample_id, text, split, seq))
        return {"sample_id": sample_id, "text": text, "split": split}

    def _reassign_split(self, conn, sample_id, split):
        self._require_labels(conn)
        _require_non_empty_str(sample_id, "sample_id")
        if split not in SPLITS:
            raise CorpusError("INVALID_INPUT",
                              "split must be one of %s" % (list(SPLITS),),
                              {"split": split})
        row = conn.execute("SELECT split FROM samples WHERE sample_id = ?",
                           (sample_id,)).fetchone()
        if row is None:
            raise CorpusError("UNKNOWN_REFERENCE",
                              "unknown sample", {"sample_id": sample_id})
        previous = row["split"]
        conn.execute("UPDATE samples SET split = ? WHERE sample_id = ?",
                     (split, sample_id))
        return {"sample_id": sample_id, "split": split, "previous_split": previous}

    def _add_duplicate(self, conn, sample_id_a, sample_id_b):
        self._require_labels(conn)
        _require_non_empty_str(sample_id_a, "sample_id_a")
        _require_non_empty_str(sample_id_b, "sample_id_b")
        if sample_id_a == sample_id_b:
            raise CorpusError("INVALID_INPUT",
                              "duplicate edge requires two distinct sample ids",
                              {"sample_id": sample_id_a})
        for sid in (sample_id_a, sample_id_b):
            row = conn.execute("SELECT 1 FROM samples WHERE sample_id = ?", (sid,)).fetchone()
            if row is None:
                raise CorpusError("UNKNOWN_REFERENCE",
                                  "unknown sample", {"sample_id": sid})
        a, b = sorted((sample_id_a, sample_id_b))
        exists = conn.execute("SELECT 1 FROM edges WHERE a = ? AND b = ?", (a, b)).fetchone()
        if exists:
            raise CorpusError("CONFLICT",
                              "duplicate edge already exists (reverse is the same edge)",
                              {"edge": [a, b]})
        conn.execute("INSERT INTO edges(a, b) VALUES (?, ?)", (a, b))
        return {"edge": [a, b], "group": self._component(conn, a)}

    def _remove_duplicate(self, conn, sample_id_a, sample_id_b):
        self._require_labels(conn)
        _require_non_empty_str(sample_id_a, "sample_id_a")
        _require_non_empty_str(sample_id_b, "sample_id_b")
        if sample_id_a == sample_id_b:
            raise CorpusError("INVALID_INPUT",
                              "duplicate edge requires two distinct sample ids",
                              {"sample_id": sample_id_a})
        a, b = sorted((sample_id_a, sample_id_b))
        cur = conn.execute("DELETE FROM edges WHERE a = ? AND b = ?", (a, b))
        if cur.rowcount == 0:
            raise CorpusError("CONFLICT",
                              "duplicate edge does not exist",
                              {"edge": [a, b]})
        return {"removed": [a, b],
                "group_of_a": self._component(conn, a),
                "group_of_b": self._component(conn, b)}

    def _submit_annotation(self, conn, sample_id, annotator, label):
        labels = self._require_labels(conn)
        _require_non_empty_str(sample_id, "sample_id")
        _require_non_empty_str(annotator, "annotator")
        if not isinstance(label, str) or label not in labels:
            raise CorpusError("INVALID_INPUT",
                              "label is not in the initialized label set",
                              {"label": label, "labels": labels})
        row = conn.execute("SELECT 1 FROM samples WHERE sample_id = ?",
                           (sample_id,)).fetchone()
        if row is None:
            raise CorpusError("UNKNOWN_REFERENCE",
                              "unknown sample", {"sample_id": sample_id})
        current = self._annotations_map(conn, [sample_id])[sample_id]
        if annotator not in current and len(current) >= 2:
            raise CorpusError(
                "CONFLICT",
                "sample already has two annotators; a third annotator is rejected",
                {"sample_id": sample_id,
                 "existing_annotators": sorted(current)})
        seq = self._next_seq(conn, "revision")
        revision_id = "rev-%06d" % seq
        replaced = annotator in current
        conn.execute(
            "INSERT INTO annotations(sample_id, annotator, label, revision_id, seq) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(sample_id, annotator) DO UPDATE SET "
            "label = excluded.label, revision_id = excluded.revision_id, seq = excluded.seq",
            (sample_id, annotator, label, revision_id, seq))
        conn.execute(
            "INSERT INTO annotation_revisions(revision_id, sample_id, annotator, label, seq) "
            "VALUES (?,?,?,?,?)",
            (revision_id, sample_id, annotator, label, seq))
        return {"sample_id": sample_id, "annotator": annotator, "label": label,
                "revision_id": revision_id, "replaced_previous": replaced}

    def _submit_arbitration(self, conn, members, evidence, label):
        labels = self._require_labels(conn)
        if not isinstance(members, (list, tuple)) or len(members) == 0:
            raise CorpusError(
                "INVALID_INPUT",
                "members must be a non-empty list of sample ids (the complete "
                "current duplicate group)")
        members = list(members)
        for m in members:
            _require_non_empty_str(m, "member")
        if len(set(members)) != len(members):
            raise CorpusError("INVALID_INPUT",
                              "members contain duplicate sample ids")
        if not isinstance(label, str) or label not in labels:
            raise CorpusError("INVALID_INPUT",
                              "label is not in the initialized label set",
                              {"label": label, "labels": labels})
        rows = self._sample_rows(conn, members)
        unknown = [m for m in members if m not in rows]
        if unknown:
            raise CorpusError("UNKNOWN_REFERENCE",
                              "arbitration references unknown samples",
                              {"unknown": unknown})
        component = self._component(conn, members[0])
        if set(component) != set(members):
            raise CorpusError(
                "CONFLICT",
                "members must equal the complete current duplicate group "
                "(connected component)",
                {"provided": sorted(members), "actual_group": component})
        ann = self._annotations_map(conn, component)
        incomplete = [m for m in component if len(ann[m]) != 2]
        if incomplete:
            raise CorpusError(
                "CONFLICT",
                "arbitration requires every group member to carry exactly two "
                "current annotations; missing dual annotation cannot be exempted",
                {"group": component,
                 "samples_lacking_dual_annotation": incomplete})
        if not isinstance(evidence, dict):
            raise CorpusError(
                "INVALID_INPUT",
                "evidence must be an object {sample_id: {annotator: revision_id}}")
        provided = {}
        for sid, per in evidence.items():
            if (not isinstance(sid, str) or not isinstance(per, dict)
                    or any(not isinstance(k, str) or not isinstance(v, str)
                           for k, v in per.items())):
                raise CorpusError(
                    "INVALID_INPUT",
                    "evidence must map sample ids to {annotator: revision_id} "
                    "string maps")
            provided[sid] = dict(per)
        current_evidence = self._evidence_of(ann, component)
        if provided != current_evidence:
            raise CorpusError(
                "CONFLICT",
                "evidence does not match the complete current group members and "
                "their current annotation revisions",
                {"expected": current_evidence, "provided": provided})
        seq = self._next_seq(conn, "arbitration")
        arb_id = "arb-%06d" % seq
        conn.execute(
            "INSERT INTO arbitrations(arb_id, member_key, evidence, label, seq) "
            "VALUES (?,?,?,?,?)",
            (arb_id, json.dumps(component, ensure_ascii=False),
             json.dumps(current_evidence, ensure_ascii=False, sort_keys=True),
             label, seq))
        return {"arb_id": arb_id, "members": component, "label": label,
                "evidence": current_evidence}

    def _deliver(self, conn, sample_ids):
        self._require_labels(conn)
        if not isinstance(sample_ids, (list, tuple)):
            raise CorpusError("INVALID_INPUT",
                              "sample_ids must be a list of sample ids (empty is legal)")
        sample_ids = list(sample_ids)
        for sid in sample_ids:
            _require_non_empty_str(sid, "sample_id")
        seen, dupes = set(), set()
        for sid in sample_ids:
            (dupes if sid in seen else seen).add(sid)
        if dupes:
            raise CorpusError("INVALID_INPUT",
                              "delivery sample set contains duplicate ids",
                              {"duplicates": sorted(dupes)})
        rows = self._sample_rows(conn, sample_ids)
        unknown = [s for s in sample_ids if s not in rows]
        if unknown:
            raise CorpusError("UNKNOWN_REFERENCE",
                              "delivery references unknown samples",
                              {"unknown": unknown})
        # 涉及的全部重复组（传递连通分量），须完整纳入
        groups = {}
        for sid in sample_ids:
            groups.setdefault(tuple(self._component(conn, sid)), None)
        requested = set(sample_ids)
        for members in groups:
            missing = [m for m in members if m not in requested]
            if missing:
                raise CorpusError(
                    "CONFLICT",
                    "delivery must include the complete duplicate group of every "
                    "involved sample",
                    {"group": list(members), "missing_members": missing})
        group_entries = []
        for members in sorted(groups):
            mlist = list(members)
            splits = {}
            for m in mlist:
                splits.setdefault(rows[m]["split"], []).append(m)
            if len(splits) != 1:
                raise CorpusError(
                    "CONFLICT",
                    "duplicate group members must share one split (train/eval)",
                    {"group": mlist, "splits": splits})
            conf = self._group_confirmation(conn, mlist)  # 未确认：抛出实际阻塞原因
            entry = {"members": mlist,
                     "split": next(iter(splits)),
                     "label": conf["label"],
                     "confirmation": conf["status"],
                     "annotations": conf["annotations"]}
            if conf["status"] == "arbitration":
                entry["arbitration"] = conf["arbitration"]  # 依赖仲裁时附仲裁内容
            group_entries.append(entry)
        label_of = {m: e["label"] for e in group_entries for m in e["members"]}
        items = [{"sample_id": sid,
                  "text": rows[sid]["text"],
                  "split": rows[sid]["split"],
                  "label": label_of[sid]} for sid in sorted(sample_ids)]
        seq = self._next_seq(conn, "delivery")
        delivery_id = "del-%06d" % seq
        payload = {"delivery_id": delivery_id,
                   "seq": seq,
                   "sample_count": len(sample_ids),
                   "items": items,
                   "groups": group_entries}
        conn.execute(
            "INSERT INTO deliveries(delivery_id, payload, seq) VALUES (?,?,?)",
            (delivery_id, json.dumps(payload, ensure_ascii=False, sort_keys=True), seq))
        return payload

    def _get_delivery(self, conn, delivery_id):
        _require_non_empty_str(delivery_id, "delivery_id")
        row = conn.execute("SELECT payload FROM deliveries WHERE delivery_id = ?",
                           (delivery_id,)).fetchone()
        if row is None:
            raise CorpusError("UNKNOWN_REFERENCE",
                              "unknown delivery id", {"delivery_id": delivery_id})
        return json.loads(row["payload"])  # 不可变快照：不受后来任何修订影响

    def _get_sample(self, conn, sample_id):
        _require_non_empty_str(sample_id, "sample_id")
        rows = self._sample_rows(conn, [sample_id])
        if sample_id not in rows:
            raise CorpusError("UNKNOWN_REFERENCE",
                              "unknown sample", {"sample_id": sample_id})
        return {"sample_id": sample_id,
                "text": rows[sample_id]["text"],
                "split": rows[sample_id]["split"],
                "annotations": self._annotations_map(conn, [sample_id])[sample_id],
                "group": self._component(conn, sample_id)}

    def _describe_group(self, conn, sample_id):
        _require_non_empty_str(sample_id, "sample_id")
        rows = self._sample_rows(conn, [sample_id])
        if sample_id not in rows:
            raise CorpusError("UNKNOWN_REFERENCE",
                              "unknown sample", {"sample_id": sample_id})
        members = self._component(conn, sample_id)
        all_rows = self._sample_rows(conn, members)
        try:
            conf = self._group_confirmation(conn, members)
            confirmation = {"status": conf["status"], "label": conf["label"]}
            if conf["status"] == "arbitration":
                confirmation["arbitration"] = conf["arbitration"]
        except CorpusError as e:
            confirmation = {"status": "unconfirmed", "code": e.code,
                            "reason": e.message, "details": e.details}
        return {"members": members,
                "splits": {m: all_rows[m]["split"] for m in members},
                "annotations": self._annotations_map(conn, members),
                "confirmation": confirmation}

    def _status(self, conn):
        labels = self._get_labels(conn)
        counts = {}
        for table in ("samples", "edges", "annotations", "annotation_revisions",
                      "arbitrations", "deliveries"):
            counts[table] = conn.execute(
                "SELECT COUNT(*) AS c FROM %s" % table).fetchone()["c"]
        return {"initialized": labels is not None, "labels": labels, "counts": counts}
