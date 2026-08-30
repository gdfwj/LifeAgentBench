---
pretty_name: "LifeAgentBench Dataset"
license: "cc-by-4.0"
language:
  - en
tags:
  - llm
  - question-answering
  - health
  - reasoning
  - tabular
  - text-generation
  - tool-use
size_categories:
  - 10K<n<100K
---

# LifeAgentBench Processed Dataset

The **LifeAgentBench Processed Dataset** is the ready-to-use question-answering collection released with [LifeAgentBench: A Multi-dimensional Benchmark and Agent for Personal Health Assistants in Digital Health](https://arxiv.org/abs/2601.13880).

LifeAgentBench evaluates the ability of large language models (LLMs) to reason over long-horizon, heterogeneous lifestyle and health records. The benchmark contains more than 22K English questions, ranging from direct factual retrieval to aggregation, comparison, consecutive-event reasoning, and trend analysis.

The processed data covers four major lifestyle dimensions:

- **Physical activity**: steps, active minutes, exercise reports, heart rate, and related daily summaries.
- **Sleep**: sleep duration, sleep stages, skin temperature, respiratory rate, and oxygen saturation.
- **Diet**: meals and food labels.
- **Emotion and stress**: emotion-related records and daily stress scores.

The dataset supports:

- **Single-user reasoning** over one participant's longitudinal records.
- **Multi-user reasoning** that compares or aggregates information across participants.
- **Single-table and multi-table reasoning** within one domain or across multiple lifestyle domains.
- **Context prompting** with relevant records embedded directly in the prompt.
- **Database-augmented prompting** in which a model generates SQL and answers from the query result.

---

## 🗂 Dataset Structure

```text
gen_data_processed/
├── original/
│   ├── single_user/
│   │   ├── single/
│   │   ├── M-sleep/
│   │   ├── M-activity/
│   │   ├── M-C2/
│   │   └── M-C4/
│   ├── multi_user/
│   │   ├── single/
│   │   └── M-C4/
│   ├── all_prompts.jsonl
│   ├── single_user.jsonl
│   └── multi_user.jsonl
├── simple/
│   ├── single_user/
│   │   ├── single/
│   │   ├── M-sleep/
│   │   ├── M-activity/
│   │   ├── M-C2/
│   │   └── M-C4/
│   └── all_prompts.jsonl
└── sql/
    ├── single_user/
    │   ├── single/
    │   ├── M-sleep/
    │   ├── M-activity/
    │   ├── M-C2/
    │   └── M-C4/
    ├── multi_user/
    │   ├── single/
    │   └── M-C4/
    ├── all_prompts.jsonl
    ├── single_user.jsonl
    └── multi_user.jsonl
```

The hierarchy represents the scope and complexity of each task:

- `single_user/single`: single-user, single-table questions.
- `single_user/M-sleep`: multi-table reasoning within the sleep domain.
- `single_user/M-activity`: multi-table reasoning within the physical-activity domain.
- `single_user/M-C2`: cross-domain reasoning involving two lifestyle dimensions.
- `single_user/M-C4`: cross-domain reasoning involving all four lifestyle dimensions.
- `multi_user/single`: multi-user reasoning over a single table.
- `multi_user/M-C4`: multi-user reasoning across all four lifestyle dimensions.

Each leaf dataset directory contains five JSONL files:

- `FQ.jsonl`: factual questions.
- `AS.jsonl`: aggregation and statistical questions.
- `CQ.jsonl`: counting and consecutive-event reasoning questions.
- `NC.jsonl`: numerical comparison questions.
- `TA.jsonl`: trend-analysis questions.

The root-level summary files combine subsets for convenient loading:

- `all_prompts.jsonl`: all questions available under the prompting setting.
- `single_user.jsonl`: all single-user questions.
- `multi_user.jsonl`: all multi-user questions.

The `simple` setting contains only single-user tasks, so it does not include a `multi_user` directory or `multi_user.jsonl`.

---

## 📦 Prompting Settings and Data Format

Each line is one JSON object. All answers must follow the output requirements stated in the corresponding question, typically as a semicolon-separated list of values without additional explanation.

### 1) Original Questions

The `original` directory contains the benchmark questions and ground-truth answers without an added table context or SQL-generation prompt.

- `Query`: natural-language question and output requirements.
- `Answer`: ground-truth answer.

Example:

```json
{
  "Query": "Which user ... achieved the largest increase in steps ...?\nOutput requirement: return 1 value(s); types (ordered): {uid}",
  "Answer": "A4F_XXXXX"
}
```

### 2) Context Prompting

The `simple` directory contains questions augmented with compact TSV views of the relevant relational tables. This setting evaluates whether a model can retrieve and reason over evidence supplied directly in its context window.

- `Query`: instructions, the question, output constraints, and compact TSV table context.
- `Answer`: ground-truth answer.

Example:

```json
{
  "Query": "You are given compact TSV views derived from multiple relational tables. ...\n\nQuestion: On the given date, how many steps did the user record?\n\n=== BEGIN TABLE `pa_daily_summary` (compact TSV, id scoped) ===\nid\tdate\tsteps\nA4F_XXXXX\t2022-05-30\t10019\n=== END TABLE ===",
  "Answer": "10019"
}
```

### 3) Database-Augmented Prompting

The `sql` directory separates SQL generation from final answer generation. The model first produces a MySQL `SELECT` statement, the query is executed against the LifeAgentBench database, and the returned result is then used to answer the original question.

- `Query_sql`: question plus MySQL schema and SQL-generation instructions.
- `Query_base`: prompt template for generating the final answer from the SQL statement and execution result.
- `Answer`: ground-truth answer.

Example:

```json
{
  "Query_sql": "Given the following MySQL table schema, write ONE SELECT statement ...",
  "Query_base": "Answer the question using the executed SQL and returned result ...",
  "Answer": "A4F_XXXXX"
}
```

---

## 🚀 Usage Examples

Install the Hugging Face `datasets` library and load the desired JSONL file directly.

```python
from datasets import load_dataset

# Original benchmark questions
original = load_dataset(
    "json",
    data_files="gen_data_processed/original/all_prompts.jsonl",
    split="train",
)

# Context-prompting questions
context = load_dataset(
    "json",
    data_files="gen_data_processed/simple/all_prompts.jsonl",
    split="train",
)

# Database-augmented questions
sql = load_dataset(
    "json",
    data_files="gen_data_processed/sql/all_prompts.jsonl",
    split="train",
)
```

To load files from the hosted dataset repository:

```python
from datasets import load_dataset

dataset = load_dataset(
    "json",
    data_files="hf://datasets/gdfwj/LifeAgentBench/gen_data_processed/original/all_prompts.jsonl",
    split="train",
)
```

The repository uses Git LFS for large files. When cloning the full dataset, install Git LFS first:

```bash
git lfs install
git clone https://github.com/gdfwj/LifeAgentBench.git
```

For benchmark evaluation scripts and database setup instructions, see the [LifeAgentBench repository](https://github.com/gdfwj/LifeAgentBench).

---

## 📑 Citation

If you use this dataset, please cite the paper:

```bibtex
@article{tian2026lifeagentbench,
  title={LifeAgentBench: A Multi-dimensional Benchmark and Agent for Personal Health Assistants in Digital Health},
  author={Tian, Ye and Wang, Zihao and Gungor, Onat and Fan, Xiaoran and Rosing, Tajana},
  journal={arXiv preprint arXiv:2601.13880},
  year={2026}
}
```

---

## ⚠️ Notes

- LifeAgentBench is intended for research and benchmarking; it is not a medical device and must not be used as a substitute for professional diagnosis or treatment.
- Participant identifiers are pseudonymous. Users should nevertheless handle all lifestyle and health-related records responsibly and follow applicable privacy and ethical requirements.
- Questions may require exact formatting. Follow the `Output requirement` included in each prompt when evaluating model predictions.
- The three top-level directories are alternative representations of related benchmark questions, not independent train, validation, and test splits.

## ⚠️ Licensing & Compliance

The dataset card declares the processed benchmark under the **Creative Commons Attribution 4.0 International (CC BY 4.0)** license. The benchmark is derived from underlying lifestyle and food data sources; users are responsible for reviewing and complying with the licenses, terms of use, privacy requirements, and citation requirements of those original sources.

The database construction workflow uses:

- [AI4FoodDB](https://github.com/AI4Food/AI4FoodDB)
- [FoodNExtDB](https://bidalab.eps.uam.es/static/AI4FoodDB/FoodNExtDB.zip)

