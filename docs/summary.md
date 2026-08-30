# LifeAgentBench

**Canonical paper title:** LifeAgentBench: Benchmarking LLMs for Long-Horizon, Cross-Dimensional Lifestyle Health Reasoning

LifeAgentBench is a question-answering benchmark for evaluating whether large language models can reason over longitudinal, heterogeneous lifestyle records. It focuses on long-horizon aggregation, cross-dimensional evidence integration, and single- and multi-user reasoning over diet, sleep, physical activity, and emotion data.

## Why It Matters

Many health and lifestyle QA benchmarks focus on a single domain, a short temporal context, or a task-specific application. Real lifestyle analysis can require aggregation over extended periods, alignment of heterogeneous signals, comparison across conditions, and cohort-level reasoning. LifeAgentBench provides a standardized, verifiable testbed for those capabilities.

LifeAgentBench and LifeAgent are research resources for lifestyle health reasoning. They are not intended for diagnosis, treatment, clinical decision making, or professional medical advice.

## Benchmark Scale

- 22,573 QA pairs
- 13,452 single-user questions
- 9,121 multi-user questions
- 100 anonymized participants in the underlying AI4FoodDB records
- 4 lifestyle dimensions: diet, sleep, physical activity, and emotion
- 13 representative LLMs evaluated in the 2026 camera-ready paper

## Task Taxonomy

LifeAgentBench uses five official reasoning types:

1. **Fact Query (FQ):** retrieve an atomic fact at a specific time.
2. **Aggregated Statistics (AS):** aggregate measurements over a longitudinal window.
3. **Numeric Comparison (NC):** compare values across time periods, lifestyle dimensions, or users.
4. **Conditional Query (CQ):** filter and evaluate records using thresholds or multi-user statistics.
5. **Trend Analysis (TA):** identify temporal changes, consecutiveness, and trends.

Questions also vary by domain scope, user scope, time horizon, and answer format. Answer formats include Yes/No, scalar numbers, short text, pairwise outputs, and multi-item lists.

## Ground-Truth Methodology

The benchmark construction pipeline normalizes and aligns lifestyle records in a relational database, instantiates questions from reusable templates, and maps each question to an executable program or database query. Execution produces a deterministic ground-truth answer. The pipeline includes programmatic checks and manual inspection.

## Key Findings

1. **Evidence retrieval is a major bottleneck.** Under Database-augmented Prompting, complete-evidence retrieval averages 28.79% across 13 models. Nine models nevertheless exceed 70% answer accuracy when complete evidence is retrieved.
2. **Complexity degrades performance.** Models struggle with aggregation-intensive questions and multi-item outputs, and accuracy declines as evidence expands across more lifestyle dimensions and users.
3. **LifeAgent improves difficult subsets.** With Qwen2.5-7B as the shared backbone, LifeAgent reaches 40.16% average accuracy on the paper's most challenging subsets, compared with 7.74% for Context Prompting and 9.43% for Database-augmented Prompting.

## LifeAgent

LifeAgent is a tool-augmented reasoning baseline introduced in the paper; it is distinct from the LifeAgentBench benchmark. Its workflow is:

1. Receive the user query.
2. Decompose it into executable sub-questions and a plan.
3. Iterate through thought, tool action, and observation while updating an evidence cache.
4. Use structured retrieval, cohort-level aggregation, and deterministic computation tools.
5. Synthesize an evidence-grounded response.

The paper's ablation study finds complementary roles for decomposition, retrieval tools, and computation tools.

## Evaluation

The paper reports Context Prompting (CP) and Database-augmented Prompting (DP) results. Normalized answer accuracy is the primary metric. DP also reports SQL validity, complete-evidence retrieval, and answer accuracy conditioned on complete evidence. Exact answer-matching and numeric-tolerance rules are defined in the paper.

## Resources

- [Project Page](https://gdfwj.github.io/LifeAgentBench/)
- [Camera-ready Paper PDF](https://gdfwj.github.io/LifeAgentBench/static/lifeagentbench-paper.pdf)
- [arXiv](https://arxiv.org/abs/2601.13880)
- [Dataset on Hugging Face](https://huggingface.co/datasets/gdfwj/LifeAgentBench)
- [Code and Evaluation README](https://github.com/gdfwj/LifeAgentBench)

## Citation

```bibtex
@inproceedings{tian-etal-2026-lifeagentbench,
  title     = {LifeAgentBench: Benchmarking LLMs for Long-Horizon,
               Cross-Dimensional Lifestyle Health Reasoning},
  author    = {Tian, Ye and Wang, Zihao and Gungor, Onat and
               Fan, Xiaoran and Rosing, Tajana},
  booktitle = {EMNLP 2026},
  year      = {2026}
}
```
