# corpus_delivery — 分类语料一致交付后端

纯 Python 标准库 + SQLite 从零实现的分类语料管理后端，承担**样本登记、双人标注修订、
重复关系维护、证据仲裁、一致交付**五项职责。无第三方依赖。

## 文件

| 文件 | 说明 |
|---|---|
| `corpus_service.py` | 核心库：`CorpusService` 类，全部业务逻辑与持久化 |
| `cli.py` | JSON Lines 命令行入口（演示/手工调用接口用） |
| `verify.py` | 独立验证脚本（仅经公开 API 行为验证，124 项断言） |

## 运行要求

- Python ≥ 3.8（仅标准库）；SQLite ≥ 3.24（使用 upsert）。开发验证环境：Python 3.11 / SQLite 3.40。

## 快速开始

```bash
cd corpus_delivery

# 1) 独立验证（退出码 0 = 全部通过；覆盖证据失效、组隔离、交错发布、
#    重启持久性、线程/跨进程并发串行等价）
python3 verify.py

# 2) 库方式调用
python3 - <<'PY'
from corpus_service import CorpusService
svc = CorpusService("/tmp/corpus.db")
print(svc.initialize_labels(["pos", "neg"]))
print(svc.register_sample("s1", "good movie", "train"))
PY

# 3) CLI 方式（JSON Lines：stdin 每行 {"method":..., "args":{...}}）
printf '%s\n' \
  '{"method":"initialize_labels","args":{"labels":["pos","neg"]}}' \
  '{"method":"register_sample","args":{"sample_id":"s1","text":"good","split":"train"}}' \
  '{"method":"status"}' | python3 cli.py /tmp/cli_demo.db
```

## 接口

所有方法同步返回统一信封（UTF-8、JSON 可序列化）：

```
成功: {"ok": true,  "data": <方法特定数据>}
失败: {"ok": false, "error": {"code": <机器可读码>, "message": <人类可读原因>, "details": {...}}}
```

任何失败调用都不改变持久化状态；一次调用多处错误时任选其一真实原因返回。

| 方法 | 参数 | 成功返回 | 语义要点 |
|---|---|---|---|
| `initialize_labels(labels)` | 非空、去重、非空字符串列表 | `{labels}` | **一次性**初始化标签集合；重复初始化拒绝（`STATE`）。其余写操作须先完成初始化 |
| `register_sample(sample_id, text, split)` | 非空 id、字符串正文、`train`/`eval` | 样本 | id 唯一；正文**不可修改**（无更新接口）；同文不同 id 各自独立保留 |
| `reassign_split(sample_id, split)` | 同上 | 新旧归属 | 归属可修订；归属变化**不**使仲裁失效 |
| `add_duplicate(a, b)` / `remove_duplicate(a, b)` | 两个已登记、不同样本 id | 边与当前组 | 无向边，反向视为同边；重复添加 / 删除不存在的边拒绝（`CONFLICT`） |
| `submit_annotation(sample_id, annotator, label)` | 非空标注者、集合内标签 | 含 `revision_id` | 每样本最多两名标注者；同人重交替换；第三人拒绝；**每次成功提交生成不复用修订身份（即使标签未变）** |
| `submit_arbitration(members, evidence, label)` | 完整组成员、`{样本:{标注者:修订id}}`、集合内标签 | 含 `arb_id` | 成员须等于当前传递连通组；每样本须恰有双人标注（**不得豁免**）；证据须与当前状态完全一致，否则拒绝；成功替换该组当前仲裁 |
| `deliver(sample_ids)` | 无重复 id 列表（**空集合合法**） | 完整交付快照 | 见下节 |
| `get_delivery(delivery_id)` | 交付标识 | 完整快照 | 成功交付不可修改，重读不受后来任何修订影响 |
| `get_sample(sample_id)` / `describe_group(sample_id)` / `status()` | — | 只读状态 | 检视用，不改变状态 |

### 重复组

已登记样本为点、重复边为边，按**传递连通分量**归组；孤立样本自成一组。边增删即改组。

### 组标签确认

- 组内每个样本恰有两人标注且全部标签一致 → **自动确认**；
- 标注齐全但不一致 → 只能由**人工仲裁**指定集合内标签；
- 任一样本缺少双人标注 → 不可确认，仲裁亦不豁免。

仲裁按"成员集"留存最新一次为当前仲裁；**使用时重新核对**完整成员及各人的当前
标注修订，仅完全匹配才有效（修订身份粒度：重标即使标签未变也使旧证据失效）；
归属变化本身不使仲裁失效。标注一致时自动确认优先，仲裁不被依赖。

### 交付

`deliver(sample_ids)` 在**单个事务**内完成全部核验与保存（核验与保存基于同一已提交
状态）：

1. 集合无重复 id、全部已登记；
2. 每个涉及样本的重复组**完整纳入**；
3. 组内归属一致（同 train 或同 eval）；
4. 组标签已确认（自动或有效仲裁）。

任一不满足即整体失败，返回实际阻塞原因，**不发布部分结果**。成功则返回：

```json
{"delivery_id": "del-000001", "seq": 1, "sample_count": 2,
 "items":  [{"sample_id": "...", "text": "...", "split": "train", "label": "pos"}],
 "groups": [{"members": ["..."], "split": "train", "label": "pos",
             "confirmation": "auto" | "arbitration",
             "annotations": {"s1": {"alice": {"revision_id": "rev-000001", "label": "pos"}}},
             "arbitration": {"arb_id": "...", "label": "...", "members": [...], "evidence": {...}}}]}
```

`arbitration` 仅在**依赖仲裁**时附上。重复交付调用每次重新核验，成功则产生独立交付
（新标识）。交付落库后不可修改，`get_delivery` 完整重读，不受后来关系/标签/归属修订影响。

### 错误码

| code | 含义 |
|---|---|
| `INVALID_INPUT` | 非法输入（空标识、集合外标签、自环边、交付集合含重复 id 等） |
| `UNKNOWN_REFERENCE` | 未知引用（样本、交付标识等） |
| `CONFLICT` | 业务冲突（重复登记、第三人标注、边已存在/不存在、组未完整纳入、组内归属不一致、标签未确认、仲裁证据失效等） |
| `STATE` | 未初始化 / 重复初始化标签集合 |
| `INTERNAL` | 非预期内部错误（状态已回滚） |

## 并发与持久化

- 每次调用 = 一个 `BEGIN IMMEDIATE` 事务：进程内经 `threading.RLock` 串行，跨进程由
  SQLite 写锁串行（`busy_timeout=30s`），故并发修订与交付**串行等价**。
- `journal_mode=WAL`、`synchronous=FULL`：已确认修订与交付重启不丢失；失败调用整体
  回滚，不暴露半次提交（交付序号只在成功提交时消耗，验证脚本据此断言）。
- 身份生成：样本/修订/仲裁/交付各自单调序号（`rev-000001`、`arb-000001`、
  `del-000001`…），事务内分配、回滚不消耗，保证**不复用**。

## 数据模型（SQLite）

`meta`（标签集合，写一次）· `seq`（序号）· `samples`（正文无更新路径）·
`edges`（`a<b` 规范化的无向边）· `annotations`（每样本每标注者当前行）·
`annotation_revisions`（全部历史修订，仅追加）· `arbitrations`（仅追加，按成员集取最新）·
`deliveries`（完整 JSON 快照，仅追加）。除 `edges` 增删与 `annotations` 当前行替换外，
无任何 UPDATE/DELETE，交付与历史修订天然不可变。

## 独立验证（verify.py，124 项断言）

1. 标签集合一次性初始化；2. 登记/归属修订；3. 边增删与传递连通归组（含"连通≠边"）；
4. 双人标注与修订身份（第三人拒绝、标签未变也出新修订）；5. 仲裁引用校验（成员不全/
越组/伪造修订/缺成员/集合外标签/缺双人标注豁免拒绝）；6. **归属变化不使仲裁失效**；
7. **证据失效**（重标后旧仲裁阻断交付，重新仲裁恢复，旧交付快照不变）；8. **组隔离**
（完整纳入、归属一致、他组未确认不阻塞本组、同文样本独立）；9. **交错发布**（交付后
状态翻转→重新核验拒绝、原交付不变、失败不消耗序号、重复调用产生独立交付）；
10. 孤立样本仲裁、空交付、未知引用；11. **重启持久性**（逐字节重读、计数持久）；
12. 线程并发与 13. 跨进程并发（无内部错误、快照自洽、交付数精确、标识唯一）。

```bash
python3 verify.py   # 末尾输出"全部验证通过。"，退出码 0
```
