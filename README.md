# LifeAgentBench

## Download and Usage  

The `eval_data/gen_data_processed` folder package contains the complete QA dataset.  
- The **original** folder provides all raw questions and answers.  
- The **simple** folder provides QA pairs under **Context Prompting**.  
- The **sql** folder provides QA pairs under **Database-augmented Prompting**.  

The dataset is hosted via **Git LFS** in this repository.  
Clone with LFS enabled to obtain the `gen_data_processed` folder:

```bash
git lfs install
```

If Git LFS bandwidth is exceeded or unavailable, you can alternatively download the `.zip` package of dataset directly from the following anonymous link: https://osf.io/download/68d24e75d6b887c60e3b7f08/?view_only=6b74757f20be4b2b8d1db5c9b5e9d551

Each folder contains multiple subfolders, where the folder name indicates the table category.  
**In addition, we provide three summary files: `all_prompts.jsonl` (all questions) is placed in the root of the `original`, `simple`, and `sql` folders; and `single_user.jsonl` / `multi_user.jsonl` (single-user and multi-user question collections) are placed in the root of the `original` and `sql` folders.** 

### Folder Structure and Description  

The processed dataset under `gen_data_processed` is organized in a **hierarchical structure** according to user scope (single-user vs. multi-user) and task complexity (single table vs. multi-table).  

- **single_user**: QA tasks for a single user.  
  - **single**: Single-table tasks from one domain (diet, activity, sleep, or emotion).  
  - **M-sleep**: Intra-domain multi-table tasks from the sleep domain (`sleep_joint`).  
  - **M-activity**: Intra-domain multi-table tasks from the activity domain (`physical_activity_joint`).  
  - **M-C2**: Cross-domain two-domain multi-table tasks (all `*_*_joint`, e.g., `activity_food_joint`, `sleep_stress_joint`).  
  - **M-C4**: Multi-table tasks across all four domains (`all_joint`).  

- **multi_user**: QA tasks requiring reasoning over multiple users.  
  - **single**: Single-table multi-user tasks (all `*_multi` datasets, e.g., `pa_active_minutes_multi`).  
  - **M-C4**: Multi-user tasks across all four domains (`all_joint_multi`).  

- **Summary files**:  
  - `all_prompts.jsonl`: all questions in this split.  
  - `single_user.jsonl`: all single-user questions in this split.  
  - `multi_user.jsonl`: all multi-user questions in this split.  
  
- Both `original` and `sql` splits contain **single_user** and **multi_user** subdirectories, and include all three summary files.  
- The `simple` split contains only **single_user** tasks and thus only has `all_prompts.jsonl` and `single_user.jsonl` at its root (no `multi_user`).  

Each dataset subfolder (e.g., `pa_active_minutes`, `activity_food_joint`, `all_joint`) contains five `.jsonl` files:  
- **FQ** (factual questions)  
- **AS** (aggregation/statistical questions)  
- **CQ** (counting/consecutive reasoning)  
- **NC** (numerical comparison)  
- **TA** (trend analysis)  

Each line in these `.jsonl` files corresponds to one QA pair.

## Evaluating Context Prompting  

The `simple` folder stores QA pairs generated with Context Prompting.  
- `"Query"`: the question.  
- `"Answer"`: the ground-truth answer.  

Run our evaluation with:  

```bash
python eval_simple.py \
  --data-root ./gen_data_processed/simple \
  --eval-root ./eval \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --max-new-tokens 32 \
  --api-key  "" 
```

After running, the original QA pairs with model's outputs will be saved under the same relative path inside your specified eval/model_name folder. In addition, an all_outputs.jsonl file will be created in that folder, which records all model outputs together with their classifications. 

To compute summary statistics, run: 

```bash
python stat_simple.py \
  eval/model_name/all_outputs.jsonl
```

This will generate a file `statistic.json` containing the overall evaluation results.

## Evaluating Database-augmented Prompting

### Build the MySQL Database

1. Create a folder `data`.
2. Download [AI4FoodDB](https://github.com/AI4Food/AI4FoodDB) and place the `dataset/` contents inside `data/`.
3. Download [FoodNExtDB.zip](https://bidalab.eps.uam.es/static/AI4FoodDB/FoodNExtDB.zip), extract it, and move into `data/FoodNExtDB/`.

The structure should look like:

```
- data
  -DS1_AnthropometricMeasurements
  -DS2_LifestyleHealth
  ...
  -DS10_AdditionalInformation
  -participant_information.csv
  -FoodNExtDB
    -A4F_10021
    ...
    -A4F_99000
- gen_data_processed
  - original
  - simple
  - sql
    - single_user
      - single
      - M-sleep
      - M-activity
      - M-C2
        - activity_food_joint 
          - AS.jsonl
          - CQ.jsonl
          - ...
          - TA.jsonl
        - activity_sleep_joint 
        - ...
        - sleep_stress_joint
      - M-C4
    - multi_user
      - single
      - M-C4
    - all_prompts.jsonl
    - multi_user.jsonl
    - single_user.jsonl
- schema.sql
- load_mysql_db.py
- load_food_db.py
```

1. Install MySQL and create a database named `MultilifeQA`.
2. Update the `user` fields in `load_mysql_db.py` and `load_food_db.py` to your local settings, then run both scripts.

### Run Evaluation

Each `.jsonl` file in the `sql` folder contains the following fields:
- `"Query_sql"`: the base question for SQL generation, where constraints need to be added.  
- `"Query_base"`: the base question for generating the final answer from the executed SQL results, where the specific SQL and result should be appended.  
- `"Answer"`: the ground-truth answer.  

For usage examples, please refer to `eval_sql.py`.

Run our evaluation with:  

```
python eval_sql.py \
  --data-root ./gen_data_processed/sql \
  --eval-root ./eval_sql \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --sql-max-new-tokens 480 \
  --ans-max-new-tokens 48 \
  --api-key  ""
```

The outputs saved under eval_sql/model_name with the same structure as the case above, with each line including:

1) the generated SQL queries,

2) whether each SQL query executed successfully,

3) the returned results from the database (for successfully executed queries), and

4) the model’s final answers.

To compute summary statistics, run:

```bash
python stat_sql.py \
  eval_sql/model_name/all_outputs.jsonl
```

This will generate a file statistic.json containing the overall evaluation results.

## How to Build Your Own Dataset

We provide not only the processed dataset, but also a **generalizable pipeline** for automatic QA generation.  
If you are interested in exploring **new reasoning problems** or **customized question types**, you can easily extend our framework:

1. **Prepare and Load Your Data into MySQL**  
   - First, organize your dataset into structured CSV or relational tables.  
   - Then, follow the workflow in `load_mysql.py`:  
     - Check how the constructor and helper functions define table names, data paths, and schema mappings.  
     - Adapt these functions for your own dataset by modifying the corresponding names and paths.  
   - Once adjusted, run the script to load your dataset into MySQL, which ensures compatibility.  

2. **Apply your own template**  
   - Define the question style and reasoning constraints you want to study.  
   - You can create new templates under the same schema (FQ, AS, CQ, NC, TA), or design entirely new categories.  
   - Once the template is prepared, simply replace the corresponding part in provided sample `build.py`. 

We encourage the community to:  
- Extend to **new reasoning challenges** (e.g., more complex questions, longitudinal trend analysis, causal reasoning).  
- Explore **new prompting strategies** beyond Context and Database-augmented Prompting.  
- Share and compare results on customized datasets built upon our framework.  

By doing so, you can continually push the boundary of how LLMs reason over complex, multi-domain data.

