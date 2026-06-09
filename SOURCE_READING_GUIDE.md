# DeepEye-SQL 源码导读笔记

本文档面向刚开始阅读 `DeepEye-SQL` 官方源码的同学，目标是快速建立从自然语言问题到最终 SQL 输出的完整代码地图。

> 结论先行：本仓库没有传统 `src/` 目录，核心代码在 `app/`；也没有顶层 `retrieval/`、`generation/`、`revision/`、`selection/` 目录，这些模块对应在 `app/pipeline/` 下。

## 1. 项目根目录结构

```text
DeepEye-SQL
├── README.md
├── pyproject.toml
├── uv.lock
├── config/
├── script/
├── runner/
├── app/
├── data/
├── results/
├── workspace/
└── exp/
```

### 重要目录

| 路径 | 作用 | 阅读优先级 |
| --- | --- | --- |
| `README.md` | 项目总览、安装、运行、评测说明 | 高 |
| `config/` | 实验配置样例，包含数据路径、LLM API、embedding、并发、采样预算 | 高 |
| `script/` | shell 自动化脚本，最重要的是 `script/run_pipeline.sh` | 高 |
| `runner/` | 每个 pipeline 阶段的可执行入口 | 高 |
| `app/config/` | TOML 配置加载和 Pydantic 配置对象 | 高 |
| `app/dataset/` | 数据集加载、`DataItem`、snapshot 读写 | 高 |
| `app/pipeline/` | 五阶段 Text-to-SQL 主流程 | 最高 |
| `app/prompt/` | Prompt 模板与模板工厂 | 高 |
| `app/db_utils/` | schema 读取、SQLite/BigQuery/Snowflake 执行 | 高 |
| `app/services/` | schema/execution/artifact service，负责缓存、断点续跑 | 高 |
| `app/vector_db/` | value retrieval 的向量索引构建与本地索引 | 中高 |
| `workspace/` | 运行时生成的 `.snapshot` 和中间产物 | 中 |
| `results/` | 发布结果和 few-shot 示例 | 中 |
| `exp/` | 额外实验、诊断、官方格式转换脚本 | 低，前期可跳过 |
| `data/` | benchmark 数据和官方评测代码 | 按需 |

### 关键文件

| 文件 | 作用 |
| --- | --- |
| `script/run_pipeline.sh` | 全流程自动执行入口 |
| `runner/preprocess_dataset.py` | 预处理数据集，生成 dataset snapshot |
| `runner/create_vector_db_parallel.py` | 为 SQLite 数据库构建 value retrieval 索引 |
| `runner/run_value_retrieval.py` | value retrieval 阶段入口 |
| `runner/run_schema_linking.py` | schema linking 阶段入口 |
| `runner/run_sql_generation.py` | SQL generation 阶段入口 |
| `runner/run_sql_revision.py` | SQL revision 阶段入口 |
| `runner/run_sql_selection.py` | SQL selection 阶段入口 |
| `runner/convert_snapshot_to_sql.py` | 将最终 snapshot 转成评测格式 |
| `runner/evaluation.py` | Spider/BIRD/Spider2 统一评测入口 |
| `app/dataset/dataset.py` | `DataItem` 和 Spider/BIRD dataset loader |
| `app/dataset/spider2_dataset.py` | Spider2-Lite / Spider2-Snow loader |
| `app/dataset/utils.py` | structured snapshot 的保存和加载 |
| `app/dataset/artifacts.py` | 各阶段 artifact 字段定义 |

## 2. 项目入口与配置流

### 全流程运行

```bash
export CONFIG_PATH=config/config-bird-example.toml
bash script/run_pipeline.sh
```

或：

```bash
bash script/run_pipeline.sh config/config-bird-example.toml
```

`script/run_pipeline.sh` 依次执行：

```text
uv run runner/preprocess_dataset.py
uv run runner/create_vector_db_parallel.py
uv run runner/run_value_retrieval.py
uv run runner/run_schema_linking.py
uv run runner/run_sql_generation.py
uv run runner/run_sql_revision.py
uv run runner/run_sql_selection.py
```

### 分阶段运行

```bash
export CONFIG_PATH=config/config-bird-example.toml

uv run runner/preprocess_dataset.py
uv run runner/create_vector_db_parallel.py
uv run runner/run_value_retrieval.py
uv run runner/run_schema_linking.py
uv run runner/run_sql_generation.py
uv run runner/run_sql_revision.py
uv run runner/run_sql_selection.py
```

### 主程序入口

严格来说，这个项目没有单个 monolithic `main.py`。主入口是：

- 全流程：`script/run_pipeline.sh`
- 阶段入口：`runner/run_*.py`
- 真实业务逻辑：`app/pipeline/*/*Runner`

例如：

```text
runner/run_sql_generation.py
 -> get_config()
 -> SQLGenerationRunner.from_config(app_config)
 -> runner.run()
```

### 命令行参数

多数 pipeline 阶段不直接使用 CLI 参数，而是通过环境变量 `CONFIG_PATH` 加载配置。

有 CLI 参数的主要脚本：

| 文件 | 参数 |
| --- | --- |
| `runner/create_vector_db_parallel.py` | `--db_parallel`、`--column_parallel` |
| `runner/convert_snapshot_to_sql.py` | `--snapshot_path`、`--output`、`--format` |
| `runner/evaluation.py` | `--snapshot_path`、`--dataset_type`、`--dataset_split`、`--max_workers`、`--timeout`、`--sql_output_dir`、`--skip_conversion` |
| `runner/benchmark_execution.py` | 性能 benchmark 相关参数 |

### 配置加载

配置入口在：

```text
app/config/config.py
```

关键函数和类：

```text
Config._get_config_path()
Config._load_config()
Config._initialize_config()
get_config()
```

配置查找逻辑：

```text
环境变量 CONFIG_PATH
  -> 如果存在，读取该 TOML
  -> 否则默认读取 config/config.toml
```

### 数据集、模型 API、Prompt 设置位置

| 内容 | 设置位置 |
| --- | --- |
| 数据集类型和路径 | `config/*.toml` 的 `[dataset]` |
| dataset snapshot 输出 | `[dataset].save_path` |
| embedding 模型/API | `[vector_database]` |
| value retrieval LLM | `[value_retrieval.llm]` |
| schema linking LLM | `[schema_linking.llm]` |
| SQL generation LLM | `[sql_generation.llm]` |
| SQL revision LLM | `[sql_revision.llm]` |
| SQL selection LLM | `[sql_selection.llm]` |
| Prompt 模板 | `app/prompt/prompt_template.py`、`app/prompt/spider2_prompt_template.py` |
| Prompt 分发 | `app/prompt/factory.py` |

## 3. 从自然语言问题到最终 SQL 的完整执行链路

### 总链路

```text
自然语言问题
  -> 数据集预处理 / schema 加载
  -> value retrieval
  -> schema linking
  -> candidate SQL generation
  -> SQL revision / execution feedback
  -> execution-aware SQL selection
  -> final SQL
```

### 详细函数调用链

#### 3.1 数据集预处理

```text
runner/preprocess_dataset.py
 -> preprocess_dataset(dataset_config)
 -> DatasetFactory.get_dataset(dataset_config)
 -> BirdDataset / SpiderDataset / Spider2LiteDataset / Spider2SnowDataset
 -> save_dataset(dataset, dataset_config.save_path)
```

关键文件：

- `app/dataset/dataset.py`
- `app/dataset/spider2_dataset.py`
- `app/dataset/utils.py`
- `app/dataset/artifacts.py`

关键数据结构：

```text
DataItem
  question_id
  question
  evidence
  gold_sql
  database_id
  database_path
  database_schema
  question_keywords
  retrieved_values
  final_linked_tables_and_columns
  sql_candidates
  sql_candidates_after_revision
  final_selected_sql
```

#### 3.2 构建 value retrieval 向量库

```text
runner/create_vector_db_parallel.py
 -> run_vector_db_creation()
 -> load_dataset(dataset_snapshot_path)
 -> make_vector_db_for_db_path()
 -> make_vector_db()
 -> _process_one_column()
 -> execute_sql_without_cache()
 -> embedding_function(batch_examples)
 -> Chroma collection / local_index
```

关键文件：

- `runner/create_vector_db_parallel.py`
- `app/vector_db/vector_db.py`
- `app/vector_db/local_index.py`

说明：

- Spider/BIRD 使用 SQLite 数据库构建 value index。
- Spider2 当前 workflow 会跳过 vector DB 构建。

#### 3.3 Value Retrieval

```text
runner/run_value_retrieval.py
 -> ValueRetrievalRunner.from_config(app_config)
 -> ValueRetrievalRunner.run()
 -> _retrieve_values_for_item(data_item)
 -> _extract_keywords(data_item)
 -> extract_keywords(question, evidence, llm)
 -> PromptFactory.format_keywords_extraction_prompt()
 -> LLMExtractor.extract_with_retry()
 -> LLM.ask()
 -> embed_keywords()
 -> _retrieve_values_for_column()
 -> retrieve_values_for_one_column() / LocalValueIndex.retrieve_values_for_column()
 -> _update_database_schema()
```

关键文件：

- `app/pipeline/value_retrieval/value_retrieval.py`
- `app/pipeline/value_retrieval/utils.py`
- `app/vector_db/local_index.py`

输入：

- question
- evidence
- database schema
- vector index

输出：

- `question_keywords`
- `retrieved_values`
- `database_schema_after_value_retrieval`

注意：

- `ValueRetrievalRunner._skip_value_retrieval_for_item()` 会跳过 Spider2。
- 关键词抽取失败时会 fallback 到 question/evidence split。

#### 3.4 Schema Linking

```text
runner/run_schema_linking.py
 -> SchemaLinkingRunner.from_config(app_config)
 -> SchemaLinkingRunner.run()
 -> _link_tables_and_columns(data_item)
 -> DirectLinker.link()
 -> ReversedLinker.link()
 -> ValueLinker.link()
 -> merge_schema_linking_results()
 -> filter_used_database_schema()
```

关键文件：

- `app/pipeline/schema_linking/schema_linking.py`
- `app/pipeline/schema_linking/linkers/direct_linker.py`
- `app/pipeline/schema_linking/linkers/reversed_linker.py`
- `app/pipeline/schema_linking/linkers/value_linker.py`
- `app/pipeline/schema_linking/utils.py`

三种 linker：

| Linker | 思路 | 关键函数 |
| --- | --- | --- |
| DirectLinker | 直接让 LLM 从 schema 中选表列 | `DirectLinker.link()` |
| ReversedLinker | 先让 LLM 生成 SQL，再从 SQL 反抽表列 | `ReversedLinker.link()`、`_extract_tables_and_columns()` |
| ValueLinker | 根据 value retrieval 的距离阈值选相关列 | `ValueLinker.link()` |

输出：

- `direct_linked_tables_and_columns`
- `reversed_linked_tables_and_columns`
- `value_linked_tables_and_columns`
- `final_linked_tables_and_columns`
- `database_schema_after_schema_linking`

当前实现特点：

- 三个 linker 在单个样本内部并行执行。
- 最终结果是简单 union merge。
- `filter_used_database_schema()` 会裁剪 schema，同时保留必要 PK/FK。

#### 3.5 Candidate SQL Generation

```text
runner/run_sql_generation.py
 -> SQLGenerationRunner.from_config(app_config)
 -> SQLGenerationRunner.run()
 -> _generate_sql(data_item)
 -> DCGenerator.generate()
 -> SkeletonGenerator.generate()
 -> ICLGenerator.generate()
 -> LLMExtractor.extract_with_retry()
 -> data_item.sql_candidates
```

关键文件：

- `app/pipeline/sql_generation/sql_generation.py`
- `app/pipeline/sql_generation/generators/base.py`
- `app/pipeline/sql_generation/generators/dc_generator.py`
- `app/pipeline/sql_generation/generators/skeleton_generator.py`
- `app/pipeline/sql_generation/generators/icl_generator.py`

三种 generator：

| Generator | 作用 |
| --- | --- |
| `DCGenerator` | 直接生成 SQL |
| `SkeletonGenerator` | 先规划 SQL skeleton，再补全 |
| `ICLGenerator` | 使用 few-shot examples 生成 SQL |

输出：

- `sql_candidates`

注意：

- 这里不执行 SQL，也不排序候选。
- 三路候选直接拼接。
- prompt schema 使用 `database_schema_after_schema_linking`。

#### 3.6 SQL Revision / Execution Feedback

```text
runner/run_sql_revision.py
 -> SQLRevisionRunner.from_config(app_config)
 -> SQLRevisionRunner.run()
 -> _revise_sql(data_item)
 -> 去重 sql_candidates
 -> _revise_one_candidate(sql, data_item)
 -> checker.check_and_revise(...)
 -> data_item.sql_candidates_after_revision
```

关键文件：

- `app/pipeline/sql_revision/sql_revision.py`
- `app/pipeline/sql_revision/checkers/base.py`
- `app/pipeline/sql_revision/checkers/*.py`

默认 checker 顺序：

```text
SyntaxChecker
JoinChecker
OrderByLimitChecker
TimeChecker
SelectChecker
MaxMinChecker
OrderByNullChecker
ResultChecker
```

Checker 类型：

| Checker | 作用 |
| --- | --- |
| `SyntaxChecker` | 执行 SQL，若报错则用执行反馈修复 |
| `ResultChecker` | 结果为空、全 NULL 或错误时用执行反馈修复 |
| `JoinChecker` | 检查特定 JOIN OR / IN 错误模式 |
| `OrderByLimitChecker` | 检查 `ORDER BY MIN/MAX ... LIMIT` 模式 |
| `TimeChecker` | 修复 `strftime(...) >= 2020` 这类日期数字引号问题 |
| `SelectChecker` | 检查 `table.*` 等 SELECT 歧义 |
| `MaxMinChecker` | 检查 MAX/MIN 嵌套查询和 LIMIT 冗余 |
| `OrderByNullChecker` | 建议 ORDER BY 列加非空约束 |

输出：

- `sql_candidates_after_revision`

注意：

- 每个唯一 SQL 候选内部是 checker 顺序执行。
- 不同唯一 SQL 候选之间并行。
- Spider2 配置样例推荐只启用 `SyntaxChecker` 和 `ResultChecker`。

#### 3.7 Execution-aware SQL Selection

```text
runner/run_sql_selection.py
 -> SQLSelectionRunner.from_config(app_config)
 -> SQLSelectionRunner.run()
 -> _select_best_sql(data_item)
 -> _get_top_k_sql_candidates(data_item)
 -> execute candidate SQL
 -> hash execution result
 -> consistency score
 -> optional shortcut
 -> _compare_sqls() pairwise LLM voting
 -> _compute_robust_win_matrix()
 -> final_selected_sql
```

关键文件：

- `app/pipeline/sql_selection/sql_selection.py`
- `app/services/execution_service.py`
- `app/pipeline/utils.py`

选择逻辑：

1. 执行每条 revised SQL。
2. 将结果表 hash 成 result hash。
3. 相同 result hash 的候选形成一致性票。
4. 按一致性得分排序，执行时间作为次排序。
5. 取 top-k。
6. 如果 top-1 一致性超过阈值，直接选。
7. 否则对 top-k 做 pairwise LLM 比较。
8. 结合 pairwise win matrix 和 consistency score 得到最终 SQL。

输出：

- `final_selected_sql`

## 4. 论文模块与代码对应

| 论文模块名 | 代码文件 | 核心类/函数 | 输入 | 输出 | 重点读 |
| --- | --- | --- | --- | --- | --- |
| Dataset Preprocessing | `runner/preprocess_dataset.py`、`app/dataset/*` | `DatasetFactory.get_dataset()`、`save_dataset()` | benchmark 原始数据 | dataset snapshot | `DataItem` 字段 |
| Value Retrieval | `app/pipeline/value_retrieval/value_retrieval.py` | `ValueRetrievalRunner._retrieve_values_for_item()` | question/evidence/schema/vector index | `retrieved_values`、增强 schema | 关键词抽取、列级检索、schema 更新 |
| Vector DB Construction | `app/vector_db/vector_db.py` | `make_vector_db()`、`_process_one_column()` | SQLite DB | Chroma/local index | 文本列过滤、embedding、local index |
| Direct Schema Linking | `app/pipeline/schema_linking/linkers/direct_linker.py` | `DirectLinker.link()` | schema profile + question | linked tables/columns | XML 解析、prompt |
| Reversed Schema Linking | `app/pipeline/schema_linking/linkers/reversed_linker.py` | `ReversedLinker.link()`、`_extract_tables_and_columns()` | generated SQL | linked tables/columns | 从 SQL 反抽 schema |
| Value-based Schema Linking | `app/pipeline/schema_linking/linkers/value_linker.py` | `ValueLinker.link()` | `retrieved_values` | linked columns | distance threshold |
| Schema Linking Merge | `app/pipeline/schema_linking/schema_linking.py` | `_link_tables_and_columns()` | 三路 linking 结果 | filtered schema | union merge、recall |
| SQL Generation | `app/pipeline/sql_generation/sql_generation.py` | `_generate_sql()` | linked schema + question | SQL candidates | 三路 generator 并行 |
| Direct SQL Generation | `app/pipeline/sql_generation/generators/dc_generator.py` | `DCGenerator.generate()` | schema profile | SQL list | DC prompt |
| Skeleton Generation | `app/pipeline/sql_generation/generators/skeleton_generator.py` | `SkeletonGenerator.generate()` | schema profile | SQL list | skeleton prompt |
| ICL Generation | `app/pipeline/sql_generation/generators/icl_generator.py` | `ICLGenerator.generate()` | few-shot + schema | SQL list | few-shot 加载 |
| SQL Revision | `app/pipeline/sql_revision/sql_revision.py` | `_revise_one_candidate()` | SQL candidates | revised candidates | checker 顺序 |
| Execution Feedback Repair | `syntax_checker.py`、`result_checker.py` | `check_and_revise()` | SQL + execution result | revised SQL | 执行反馈 prompt |
| Rule-based Checker Repair | `join_checker.py` 等 | `_check_*()` | SQL text | suggestion / revised SQL | hand-written patterns |
| SQL Selection | `app/pipeline/sql_selection/sql_selection.py` | `_select_best_sql()` | revised candidates | final SQL | 一致性、执行、pairwise voting |
| Prompt Construction | `app/prompt/factory.py`、`app/prompt/*.py` | `PromptFactory.*` | schema/question/hint/sql | prompt string | 模板和 Spider2 分支 |
| Schema Profile Compression | `app/services/schema_service.py` | `build_prompt_with_progressive_schema_stripping()` | schema dict | prompt under token budget | stripping levels |
| Execution Service | `app/services/execution_service.py` | `execute()`、`measure_time()`、`hash_result()` | data item + SQL | execution result/hash/time | 缓存、云数据库 |

## 5. 适合做论文改进的位置

### 5.1 Schema Linking / Evidence Acquisition

最值得优先考虑。

当前实现：

```text
DirectLinker + ReversedLinker + ValueLinker -> union merge
```

可改进点：

- 给 linked table/column 增加置信度，而不是简单 union。
- 对 direct/reversed/value 三路结果做 ranker 或 verifier。
- 引入 evidence acquisition：表描述、业务指标口径、外部文档、SQL 历史日志。
- 对 schema linking 错误做可解释诊断。
- 对 Spider2 的 identical schema / wildcard table 做更强建模。

建议切入文件：

- `app/pipeline/schema_linking/schema_linking.py`
- `app/pipeline/schema_linking/linkers/*.py`
- `app/db_utils/schema.py`
- `app/services/schema_service.py`

### 5.2 Value Retrieval

当前实现：

```text
LLM keyword extraction -> keyword embedding -> 每个文本列检索 top values
```

可改进点：

- 问题、schema、列描述联合检索。
- entity linking 与 value retrieval 统一建模。
- 企业场景加入同义词、指标名、枚举字典。
- Spider2 当前跳过 value retrieval，可考虑云数据库 sample rows / metadata retrieval。
- 用 reranker 替代纯向量距离。

建议切入文件：

- `app/pipeline/value_retrieval/value_retrieval.py`
- `app/pipeline/value_retrieval/utils.py`
- `app/vector_db/vector_db.py`
- `app/vector_db/local_index.py`

### 5.3 Candidate SQL Generation

当前实现：

```text
DCGenerator + SkeletonGenerator + ICLGenerator
```

可改进点：

- 动态选择 generator，而不是固定三路全跑。
- 基于 schema linking 不确定性调整采样预算。
- 增加 decomposition generator，例如先生成 query plan / relational algebra。
- 给候选 SQL 增加 provenance，方便 selection 和 revision 使用。

建议切入文件：

- `app/pipeline/sql_generation/sql_generation.py`
- `app/pipeline/sql_generation/generators/*.py`
- `app/prompt/prompt_template.py`

### 5.4 SQL Revision

当前实现：

```text
每条唯一候选 SQL 顺序经过多个 checker
```

可改进点：

- 动态 checker ordering。
- 基于执行错误类型选择 checker。
- 用 learned verifier 替代部分正则规则。
- 多轮 execution feedback repair。
- 记录 revision trace，用于论文分析。

建议切入文件：

- `app/pipeline/sql_revision/sql_revision.py`
- `app/pipeline/sql_revision/checkers/*.py`
- `app/services/execution_service.py`

### 5.5 Execution-aware Selection

非常适合做论文 idea。

当前实现：

```text
执行结果 hash -> consistency score -> top-k -> pairwise LLM comparison
```

可改进点：

- 引入 semantic verifier。
- 结合 execution result shape、空值比例、列名匹配程度。
- 使用 question-aware result validation。
- 对 pairwise voting 做校准。
- 把 latency/cost 纳入 selection objective。

建议切入文件：

- `app/pipeline/sql_selection/sql_selection.py`
- `app/pipeline/utils.py`
- `app/services/execution_service.py`

### 5.6 Prompt Construction

当前实现集中，容易做实验。

可改进点：

- schema profile 结构重写。
- prompt 自动压缩策略优化。
- 加入 few-shot retrieval，而不是固定 few-shot 文件。
- 对 Spider2 / cloud SQL dialect 做更细粒度模板。

建议切入文件：

- `app/prompt/factory.py`
- `app/prompt/prompt_template.py`
- `app/prompt/spider2_prompt_template.py`
- `app/services/schema_service.py`

### 5.7 Cost / Latency 优化

当前可控变量：

- `n_parallel`
- `n_internal_parallel`
- `*_sampling_budget`
- `filter_top_k_sql`
- `shortcut_consistency_score_threshold`
- execution cache
- local index backend

可改进点：

- adaptive sampling。
- early exit。
- LLM 调用复用。
- 批量 prompt / batch inference。
- 成本感知 selector。

建议切入文件：

- `app/config/config.py`
- `app/llm/llm.py`
- `app/llm_extractor/extractor.py`
- `app/services/execution_service.py`
- `runner/benchmark_execution.py`

### 5.8 多轮交互 / 企业智能问数

当前项目是单轮 Text-to-SQL pipeline。

可改进点：

- `DataItem` 扩展 conversation history。
- 引入用户澄清问题。
- 接入企业指标字典、权限、数据血缘。
- 结果解释、SQL 可视化、审计日志。
- 将 schema linking 扩展成 business semantic linking。

建议切入文件：

- `app/dataset/dataset.py`
- `app/pipeline/schema_linking/*`
- `app/prompt/*`
- 新增 memory / semantic layer service

## 6. 2 小时快速读源码路线图

### 0-15 分钟：建立项目框架

先看：

- `README.md`
- `script/run_pipeline.sh`
- `config/config-bird-example.toml`
- `app/config/config.py`

重点：

- `CONFIG_PATH` 如何加载。
- 每阶段 `save_path` 如何串起来。
- 每个阶段的 LLM 是否可以不同。

暂时跳过：

- `exp/`
- `results/`
- `data/Spider2-official/methods/*`

### 15-30 分钟：理解数据载体和 snapshot

先看：

- `app/dataset/dataset.py`
- `app/dataset/artifacts.py`
- `app/dataset/utils.py`
- `app/services/artifact_store.py`

重点函数：

- `DataItem`
- `DataItem.get_stage_artifact()`
- `DataItem.apply_stage_artifact()`
- `save_dataset()`
- `load_dataset()`
- `ArtifactStore.record_item()`
- `load_stage_dataset()`

需要理解：

```text
每个阶段读上一个阶段 snapshot
每个阶段写自己的 snapshot
ArtifactStore 支持中途 checkpoint
```

### 30-50 分钟：读 value retrieval

先看：

- `app/pipeline/value_retrieval/value_retrieval.py`
- `app/pipeline/value_retrieval/utils.py`
- `app/vector_db/vector_db.py`

重点函数：

- `ValueRetrievalRunner.run()`
- `_retrieve_values_for_item()`
- `_extract_keywords()`
- `_retrieve_values_for_column()`
- `_update_database_schema()`
- `extract_keywords()`
- `retrieve_values_for_one_column()`
- `make_vector_db()`

可暂时跳过：

- Chroma 细节。
- `qwen_embedding_function.py`。

建议断点 / 日志：

- `_retrieve_values_for_item()` 开头。
- `extract_keywords()` 返回处。
- `_update_database_schema()` 结束处。

### 50-70 分钟：读 schema linking

先看：

- `app/pipeline/schema_linking/schema_linking.py`
- `app/pipeline/schema_linking/linkers/direct_linker.py`
- `app/pipeline/schema_linking/linkers/reversed_linker.py`
- `app/pipeline/schema_linking/linkers/value_linker.py`

重点函数：

- `SchemaLinkingRunner._link_tables_and_columns()`
- `DirectLinker.link()`
- `ReversedLinker.link()`
- `ReversedLinker._extract_tables_and_columns()`
- `ValueLinker.link()`
- `merge_schema_linking_results()`
- `filter_used_database_schema()`

建议断点 / 日志：

- 三个 linker 返回后。
- `merged_linked_tables_and_columns` 生成后。
- `database_schema_after_schema_linking` 生成后。

### 70-90 分钟：读 SQL generation 和 prompt

先看：

- `app/pipeline/sql_generation/sql_generation.py`
- `app/pipeline/sql_generation/generators/*.py`
- `app/prompt/factory.py`
- `app/prompt/prompt_template.py`

重点函数：

- `SQLGenerationRunner._generate_sql()`
- `BaseSQLGenerator._generate_with_progressive_stripping()`
- `DCGenerator.generate()`
- `SkeletonGenerator.generate()`
- `ICLGenerator.generate()`
- `PromptFactory.format_*()`

建议断点 / 日志：

- 每个 generator 的 `final_prompt`。
- `all_sql_candidates` 返回处。

### 90-110 分钟：读 SQL revision 和 selection

先看：

- `app/pipeline/sql_revision/sql_revision.py`
- `app/pipeline/sql_revision/checkers/syntax_checker.py`
- `app/pipeline/sql_revision/checkers/result_checker.py`
- `app/pipeline/sql_selection/sql_selection.py`

重点函数：

- `SQLRevisionRunner._revise_sql()`
- `SQLRevisionRunner._revise_one_candidate()`
- `SyntaxChecker.check_and_revise()`
- `ResultChecker.check_and_revise()`
- `SQLSelectionRunner._get_top_k_sql_candidates()`
- `SQLSelectionRunner._select_best_sql()`
- `SQLSelectionRunner._compare_sqls()`

建议断点 / 日志：

- 每个 checker 前后的 SQL。
- `execution_result.result_type`。
- `top_k_sql_candidates`。
- `final_selected_sql`。

### 110-120 分钟：读服务层

先看：

- `app/llm/llm.py`
- `app/llm_extractor/extractor.py`
- `app/services/schema_service.py`
- `app/services/execution_service.py`

重点函数：

- `LLM.ask()`
- `LLMExtractor.extract_with_retry()`
- `SchemaService.build_prompt_with_progressive_schema_stripping()`
- `ExecutionService.execute()`
- `ExecutionService.hash_result()`

## 7. 源码阅读地图

```text
config/*.toml
  |
  v
app/config/config.py
  |
  v
runner/*.py
  |
  v
app/dataset/*.py
  |
  v
workspace/dataset/*.snapshot
  |
  v
app/pipeline/value_retrieval
  |
  v
workspace/value_retrieval/*.snapshot
  |
  v
app/pipeline/schema_linking
  |
  v
workspace/schema_linking/*.snapshot
  |
  v
app/pipeline/sql_generation
  |
  v
workspace/sql_generation/*.snapshot
  |
  v
app/pipeline/sql_revision
  |
  v
workspace/sql_revision/*.snapshot
  |
  v
app/pipeline/sql_selection
  |
  v
workspace/sql_selection/*.snapshot
  |
  v
runner/convert_snapshot_to_sql.py
  |
  v
runner/evaluation.py
```

## 8. 推荐第一批断点

| 位置 | 为什么打 |
| --- | --- |
| `ValueRetrievalRunner._retrieve_values_for_item()` | 看 question 如何变成 keywords 和 retrieved values |
| `ValueRetrievalRunner._update_database_schema()` | 看 value examples 如何写回 schema |
| `SchemaLinkingRunner._link_tables_and_columns()` | 看 direct/reversed/value 三路结果 |
| `filter_used_database_schema()` | 看 schema 如何被裁剪 |
| `SQLGenerationRunner._generate_sql()` | 看三路候选 SQL |
| `SQLRevisionRunner._revise_one_candidate()` | 看 checker 如何逐步修改 SQL |
| `SyntaxChecker.check_and_revise()` | 看执行错误如何反馈给 LLM |
| `ResultChecker.check_and_revise()` | 看空结果/异常结果如何修复 |
| `SQLSelectionRunner._get_top_k_sql_candidates()` | 看执行一致性如何算 |
| `SQLSelectionRunner._select_best_sql()` | 看最终 SQL 如何选出 |
| `LLMExtractor.extract_with_retry()` | 看 LLM 输出如何解析和重试 |
| `SchemaService.build_prompt_with_progressive_schema_stripping()` | 看 prompt 超长时如何压缩 |

## 9. 暂时可以跳过的代码

初读阶段可以先跳过：

- `exp/*`
- `runner/benchmark_execution.py`
- `runner/preprocess_dail_few_shots.py`
- `results/*`
- `data/Spider2-official/methods/*`
- `app/vector_db/qwen_embedding_function.py`
- `app/logger/*`

等你开始做性能优化、复现实验、Spider2 官方评测或本地 embedding 适配时再回来读。

## 10. 一个小提醒

如果本地存在真实运行配置，比如 `config/config-bird.toml`，提交论文 artifact 或开源前需要检查里面是否包含真实 API key、base URL 或其他敏感信息。配置样例文件 `config/*-example.toml` 才适合公开引用。
