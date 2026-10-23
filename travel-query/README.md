# Travel Query Classifier

A small local classifier that decides whether a user's message is a **TRAVEL** request the TravelAgentic agent can handle, or a **NOT_TRAVEL** message that can be answered with a static reply without calling a paid LLM.

| File | Purpose |
| --- | --- |
| `travel_query_classifier.ipynb` | Google Colab notebook: data preparation, baseline training, DistilBERT + LoRA fine-tuning, validation, final test, example predictions, export and reload. |
| `README.md` | This document. |
| `.gitignore` | Keeps trained models, checkpoints and archives (`travel_query_output/`, `content/`, `travel_query_gate/`, `*.zip`, `*.safetensors`, `*.joblib`), Jupyter autosaves and local OS/editor files out of Git. |

No trained weights are committed to this folder. The notebook creates them at runtime. Section 7.1 summarizes the measured results of the Colab run that produced the exported archive.

## 1. Objective and relationship to the Travel Agent

The agent in `backend/services/agent/graph.py` sends every conversation to the `extract` node first. `extract` calls the LLM (`services/agent/llm.py → generate`) to turn the message into `TripConstraints`. If required fields are missing, the graph goes on to `clarify` (a second LLM call). An unrelated message such as "What is Python?" therefore costs at least two LLM calls before the user learns the agent cannot help.

The classifier is meant to run **before `extract`** on the first message of a conversation:

- **TRAVEL or uncertain** → forward to `extract` unchanged (existing behavior);
- **confidently NOT_TRAVEL** → static "I can help you plan trips…" reply, no LLM call.

Blocking a real travel request is worse than paying for one unnecessary LLM call, so the gate is tuned for **high travel recall** and forwards anything uncertain. The production graph has **not** been modified; Section 9 describes the proposed integration.

## 2. Dataset and labels

**Source:** [CLINC150](https://huggingface.co/datasets/clinc/clinc_oos) (Larson et al., 2019, *An Evaluation Dataset for Intent Classification and Out-of-Scope Prediction*), official Hugging Face upload `clinc/clinc_oos`, configuration `plus`, revision `155b9c710419136e17307b80d0a13e68cd46b4ec`, license CC BY 3.0. The schema was verified before writing the label mapping: columns `text` (string) and `intent` (class label with 151 names: 150 intents plus `oos`), and the original `train` / `validation` / `test` splits (15,250 / 3,100 / 5,500 rows).

**Binary label policy.** The agent collects origin, destination, fly/drive, one-way/round trip, dates, budget, travelers, rental car, layovers, activities and hotel preferences, and is planned to return flights, hotels, rental cars and price trends. The mapping follows those capabilities:

| Binary label | CLINC150 intents | Reason |
| --- | --- | --- |
| TRAVEL (1) | `book_flight`, `book_hotel`, `car_rental`, `travel_suggestion` | Requests the agent is built to handle. |
| NOT_TRAVEL (0) | The other 145 intents and `oos` | Includes *travel-adjacent* intents the agent has no tool or data for: `flight_status`, `lost_luggage`, `carry_on`, `international_visa`, `vaccines`, `travel_alert`, `travel_notification`, `exchange_rate`, `plug_type`, `timezone`, `translate`, `international_fees`, `weather`, `distance`, `directions`, `uber`. The notebook reports how often each is forwarded. |
| Excluded | `pto_request` | Most of its examples are HR leave requests: 66 of the 100 training examples say "pto", and most others ask how to request vacation time. A few read as trip requests our agent should accept ("how do i book a vacation", "i'd like to go on vacation", "can you schedule a vacation for next week"). Labeling the intent NOT_TRAVEL would teach the model to reject vacation requests, so it is removed from training and from the metrics. |

**Split separation.** The notebook compares texts across splits ignoring case, whitespace and punctuation. It drops the 49 training rows that match a validation or test text and the 4 validation rows that match a test text, then asserts that no text is shared between splits. All 53 are NOT_TRAVEL small talk ("hey what's up" vs "hey whats up", "thanks for your help"). The test split is left complete.

| Split | TRAVEL | NOT_TRAVEL | Used for |
| --- | ---: | ---: | --- |
| train | 400 | 14,701 | fitting both models |
| validation | 80 | 2,996 | LoRA checkpoint, rejection thresholds, model selection, error analysis |
| test | 120 | 5,350 | one final evaluation after all choices are fixed |

**Limitations of the mapping:**

- CLINC150 is a general voice-assistant dataset, not TravelAgentic traffic. Its TRAVEL examples are short, lowercase, single-intent requests.
- Phrasing that is central to this app is rare or misleading in the training data. In the CLINC150 `plus` training split, the word "vacation" occurs in 2 TRAVEL vs 210 NOT_TRAVEL examples (mostly PTO intents), "plan" in 1 vs 89 (mostly insurance plans), and "trip" in 6 vs 24. "budget", "itinerary" and "business trip" never occur, and "cheap" occurs only inside "cheapest"/"cheaper" (3 TRAVEL, 1 NOT_TRAVEL). Benchmark scores therefore say little about queries such as "Plan a cheap trip to Japan"; the notebook's example predictions show how each model handles them.
- Treating travel-adjacent intents as NOT_TRAVEL is a product decision. If the agent adds, for example, a visa or weather tool, move those intents into `TRAVEL_INTENTS` and retrain.
- Positive counts are small (80 validation, 120 test), so recall estimates are coarse. The notebook reports 95% Wilson intervals.

## 3. Models

**Baseline: TF-IDF + Logistic Regression.** Word unigrams and bigrams (`min_df=2`, sublinear TF) feed a class-balanced logistic regression (scikit-learn). It trains in well under a second on CPU, and its export is about 360 KB.

**Transfer learning: DistilBERT + LoRA.** `distilbert/distilbert-base-uncased` (revision `12040acc…`): a pretrained 6-layer, 768-dimensional, 12-head English encoder (66M parameters). `AutoModelForSequenceClassification` adds a new `pre_classifier` (768→768) and `classifier` (768→2) head. PEFT LoRA (`r=8`, `lora_alpha=16`, `lora_dropout=0.1`) adds trainable low-rank matrices to the attention query and value projections (`q_lin`, `v_lin`). All pretrained weights stay frozen. Because the head is newly initialized, it is listed in `modules_to_save`, so it is fully trained and saved with the adapter. The notebook asserts that only LoRA and head parameters are trainable: 739,586 of 67.7M parameters (1.09%).

## 4. Training methodology

- **Seeds:** `SEED = 42` via `transformers.set_seed` (Python, NumPy, PyTorch CPU and CUDA). GPU kernels are not forced to be deterministic, so GPU runs can differ slightly.
- **Pinned inputs:** dataset and base-model revisions are pinned, and package versions are pinned in the install cell and recorded in `metadata.json`.
- **Baseline:** `fit` on the training split only.
- **LoRA fine-tuning:** a supervised loop with Hugging Face `Trainer`. Tokenized queries (max 64 tokens; the longest training query is 33) go through cross-entropy loss and AdamW (learning rate 2e-4, weight decay 0.01, batch size 32, linear decay) for 3 epochs over the full training split. Validation F1 is evaluated after every epoch and the best epoch is restored. Mixed precision is fp16 on a T4 and bf16 on Ampere or newer, for training only; CPU runs use fp32. After training, the notebook removes the autocast wrapper that `accelerate` leaves on the model. Every LoRA score used for the threshold, model selection, test metrics and examples is therefore float32, the same precision the exported model uses on the backend.
- **Rejection threshold (validation only):** the highest threshold whose validation travel recall is at least `TARGET_TRAVEL_RECALL = 0.98`. It is moved halfway to the next lower validation score, so GPU/CPU float differences do not flip decisions, and capped at 0.5, so a query the model rates as more likely TRAVEL is never rejected.
- **Model selection (validation only, fixed before testing):** both thresholds meet the recall target, so the model with the higher validation non-travel rejection rate is selected; ties go to the cheaper baseline.
- **Test:** scored once, in Section 9 of the notebook. The non-selected model's test scores are shown for comparison only.

## 5. Running it in Google Colab

GPU: any CUDA GPU; a free **T4** is enough. Without a GPU, the notebook trains and evaluates only the baseline (LoRA is skipped) unless you set `TRAIN_LORA = True` in Section 1.

1. Open <https://colab.research.google.com> → **File → Upload notebook** → select `travel-query/travel_query_classifier.ipynb`. If this notebook, or an earlier version of it, already ran in the current runtime, choose **Runtime → Disconnect and delete runtime** first, so that installation starts from Colab's unmodified image.
2. **Runtime → Change runtime type → Hardware accelerator: T4 GPU → Save.**
3. **Runtime → Run all.** The first code cell removes `torchao` and installs the pinned packages:
   ```
   %pip uninstall -y -q torchao
   %pip -q install "transformers==5.19.0" "peft==0.21.2" "accelerate==1.15.0" "datasets==4.8.5" "scikit-learn==1.9.1" "joblib==1.6.0"
   ```
   In a fresh runtime no restart is needed. See [Runtime restart](#runtime-restart) for when one is.
4. Check the Section 1 output: it must show `Device: cuda (Tesla T4)` and `TRAIN_LORA: True`, and the printed versions must match the [table below](#supported-dependency-versions). If it shows `cpu`, the GPU runtime was not selected.
5. Check the Section 6 output as described in [Confirming LoRA initialization](#confirming-lora-initialization).
6. Read the outputs in order:
   - Section 3: class counts;
   - Section 5: baseline validation metrics;
   - Section 7: LoRA training log (eval F1 per epoch), validation metrics and, on a GPU, peak GPU memory;
   - Section 8: the comparison table, the selected model, the validation false rejects and the forward rates of travel-adjacent intents;
   - Section 9: the final test metrics;
   - Section 10: the example predictions and the `requirement examples misrouted` lines.
7. Section 11 writes `/content/travel_query_output/travel_query_gate/` and `/content/travel_query_output/travel_query_gate.zip`. Files under `/content` are deleted when the runtime ends, so keep the archive with one of:
   ```python
   from google.colab import files
   files.download("/content/travel_query_output/travel_query_gate.zip")
   ```
   ```python
   from google.colab import drive
   drive.mount("/content/drive")
   !cp /content/travel_query_output/travel_query_gate.zip /content/drive/MyDrive/
   ```
8. Record `travel_query_gate/metadata.json`: it contains the measured metrics, thresholds and versions of your run.

The dataset and model are public, so no Hugging Face token is required. Colab may warn about unauthenticated requests; that is harmless. In the Colab T4 run in Section 7.1, LoRA training (3 epochs, including per-epoch validation) took 108 s, as recorded in `metadata.json` (`train_seconds`).

### Supported dependency versions

| Package | Version | Source in Colab |
| --- | --- | --- |
| Python | 3.13 | Colab |
| torch | 2.11.0+cu130 (any ≥ 2.5) | Colab, not changed |
| transformers | 5.19.0 | install cell (Colab ships 5.18.0) |
| peft | 0.21.2 | install cell (Colab ships 0.21.1) |
| accelerate | 1.15.0 | same as Colab |
| datasets | 4.8.5 | same as Colab. Do **not** use 5.1.0: it requires `pyarrow>=24` |
| pyarrow | 23.0.1 | Colab, not changed (`cudf-cu13 26.6.0` requires `pyarrow<24`) |
| scikit-learn | 1.9.1 | install cell (Colab ships 1.6.1) |
| joblib | 1.6.0 | same as Colab |
| numpy / pandas | 2.1.3 / 2.2.3 | Colab, not changed |
| torchao | not installed | removed by the install cell (Colab ships 0.10.0; PEFT requires ≥ 0.16.0 if it is present) |

**What the install cell changes.** These versions were checked against Colab's **GPU** image as published in [googlecolab/backend-info](https://github.com/googlecolab/backend-info) (`pip-freeze.gpu.txt`, commit `e39694e`, 2026-10-03: Python 3.13.15, torch 2.11.0+cu130, CUDA 13.0). Starting from that image, the install cell removes `torchao` and upgrades only `transformers`, `peft` and `scikit-learn`. PyArrow, PyTorch, the CUDA packages, NumPy and pandas are left alone. The declared requirements of 717 of the image's 721 packages were evaluated with Colab's platform markers after these changes. The other four are not on PyPI: `google-colab`, `jupyter_kernel_gateway`, `en-core-web-sm` and an Ubuntu build of `Mako`. The changes introduce no conflict, and no package in the image declares a dependency on `torchao`. The only problem reported, `ipython 7.34.0 requires jedi`, is already present in Colab's published image and does not affect this notebook. An earlier version of this README checked Colab's CPU package list (`pip-freeze.txt`), which does not contain the RAPIDS packages such as `cudf-cu13`. That is why the PyArrow conflict below was missed. Colab's image changes over time, so compare the versions printed in Section 1 with the table above.

### Runtime restart

- **Fresh runtime:** no restart is needed, because nothing the install cell changes has been imported yet. IPython prints `Note: you may need to restart the kernel to use updated packages.` after every `%pip` command; that note alone does not mean a restart is needed.
- **Restart required** if Colab shows a *Restart session* prompt or warns that packages were imported before they were reinstalled, if Section 1 stops with `torchao is still importable`, or if you ran cells that import these packages before the install cell (for example, an earlier version of this notebook in the same runtime). A package upgraded after it was imported can keep running a mix of old and new code while reporting the new version, so do not rely on the printed versions alone. Choose **Runtime → Restart session**, then **Runtime → Run all**. A restart keeps the installed packages and the files under `/content`, but clears all Python variables, so always rerun from the top. The install cell is safe to run again: `pip` reports that `torchao` is not installed and the pinned packages are already satisfied.

### Confirming LoRA initialization

The Section 6 cell (`get_peft_model(load_base_model(), lora_config)`) has succeeded when:

1. It prints a `DistilBertForSequenceClassification LOAD REPORT` with exactly these keys. This is expected for sequence classification, not an error:
   - `UNEXPECTED`: `vocab_transform.weight`, `vocab_transform.bias`, `vocab_layer_norm.weight`, `vocab_layer_norm.bias`, `vocab_projector.bias`. These form the masked-language-model head used in pretraining; classification does not use it, so it is dropped.
   - `MISSING`: `pre_classifier.weight`, `pre_classifier.bias`, `classifier.weight`, `classifier.bias`. This is the new classification head. It is randomly initialized at this point, then trained and saved through `modules_to_save`.
2. It prints `trainable params: 739,586 || all params: 67,694,596 || trainable%: 1.0925`.
3. No `AssertionError` is raised (only LoRA and head parameters are trainable, and both head layers are trainable), and the cell ends by printing the tokenized training set (`num_rows: 15101`).

Section 11 prints the same load report when it reloads the base model. The adapter then restores the trained head, and the reload check asserts that the reloaded scores match.

### Troubleshooting

**`cudf-cu13 26.6.0 requires pyarrow<24,>=19.0.0, but you have pyarrow 26.0.0 which is incompatible.`**
`datasets` 5.1.0 requires `pyarrow>=24.0.0`, while Colab's GPU image ships `pyarrow` 23.0.1 for `cudf-cu13` 26.6.0, which requires `pyarrow<24`. No PyArrow version satisfies both, so the notebook now pins `datasets==4.8.5` (Colab's own version, which needs `pyarrow>=21`) and does not touch PyArrow. If the message still appears, the runtime still holds packages from the earlier install cell. Choose **Runtime → Disconnect and delete runtime**, then **Run all**.

**`ImportError: Found an incompatible version of torchao. Found version 0.10.0, but only versions above 0.16.0 are supported`**
Colab preinstalls `torchao` 0.10.0. Whenever PEFT 0.21.x (both Colab's 0.21.1 and the pinned 0.21.2) creates LoRA layers, it checks the installed `torchao` and raises this error for versions below 0.16.0. `torchao` is only needed for `torchao`-quantized base models, and this notebook trains LoRA on ordinary full-precision `nn.Linear` layers, so the install cell uninstalls it. If you still see the error, rerun the install cell, then **Runtime → Restart session → Run all**. `!pip show torchao` should then print `WARNING: Package(s) not found: torchao`. Upgrading `torchao` instead is not recommended: the version built for Colab's torch 2.11 is 0.17.0 (per torchao's compatibility table), a compiled extension that was not tested with this notebook.

**Section 1 stops with `torchao is still importable`:** `torchao` was imported before the install cell removed it. See [Runtime restart](#runtime-restart).

**`AssertionError: lora: reloaded scores differ by 0.0026702880859375` (Section 11, previous notebook version)**
`accelerate` applies fp16 (T4) and bf16 mixed precision by wrapping the model's `forward` in autocast, and `Trainer` leaves that wrapper on the trained model. The previous notebook therefore computed the in-memory LoRA validation scores, and the threshold chosen from them, in half precision. The exported adapter, by contrast, is reloaded and scored in float32 on CPU, as the backend does. The adapter and the trained classification head were saved and reloaded correctly; the two copies were simply scored at different precisions. The difference matters:
- With bf16 mixed precision on CPU (the same code path, in a 40-step test run), the old check *passed* with a maximum difference of 3.1e-4 even though 13 validation queries changed route.
- Emulating fp16 autocast on the fully trained local adapter moved the validation threshold (0.064315 → 0.064184) and changed the route of one validation query.

The fix: Section 7 now calls `trainer.accelerator.unwrap_model(trainer.model, keep_fp32_wrapper=False)` after training, so every LoRA score in the notebook is float32. Training itself still uses mixed precision. The reload check keeps its 1e-3 tolerance, now also fails if any validation route changes, and lists the affected queries. In the same CPU test, the fixed notebook reloaded with a maximum and mean difference of 0.0 and no routing changes. Use the corrected notebook and **Run all** (see [Resuming in Colab](#resuming-in-colab)). If the check still fails, read the printed `reload_check` values and the listed queries. With float32 on both sides, a difference near 1e-3 or a routing change points to a save or reload problem (base-model revision, adapter or head), not to precision.

**Runtime limits and likely failures:**

- **No GPU assigned.** Free Colab GPUs depend on availability and usage limits. Section 1 then prints `cpu` and LoRA is skipped. Try again later, or set `TRAIN_LORA = True` to train on CPU. That took about 200 s on a 10-core laptop CPU; Colab's CPU runtime has fewer cores and was not timed.
- **Disconnects.** Idle or long sessions are disconnected, and everything under `/content` is lost, including the trained adapter. Download the archive (step 7) as soon as Section 11 finishes.
- **Memory.** The local CPU run peaked at 1.6 GiB of RAM for the whole notebook, a small fraction of a standard Colab runtime. Section 7 of the notebook prints peak GPU memory on a GPU run; the value from the Colab run in Section 7.1 below was not preserved, because the executed notebook was not saved. If CUDA runs out of memory anyway, lower `per_device_train_batch_size` in Section 7.
- **Downloads.** The dataset and the base model (about 270 MB) come from the Hugging Face Hub at pinned revisions. If the Hub is unreachable or rate-limits the runtime, Section 2 or Section 6 fails; rerun that cell.
- **Restart prompt.** If Colab asks to restart after the install, restart and choose *Run all* again (see [Runtime restart](#runtime-restart)).

### Resuming in Colab

- **After the earlier failure** (the PyArrow warning, or the `torchao` `ImportError` in Section 6): LoRA training never started, so there is nothing to resume. Upload the corrected notebook, choose **Runtime → Disconnect and delete runtime**, select the T4 GPU and choose **Run all**.
- **After the reload `AssertionError` in Section 11:** the trained weights were fine, but that run's validation scores, threshold, model selection and test metrics came from half-precision scoring, and `metadata.json` and the archive were never written. Do not reuse files from that run. Upload the corrected notebook and choose **Run all**. LoRA is trained again (3 epochs on the T4) and every score is recomputed in float32.
- **After Runtime → Restart session:** Python variables are gone, but installed packages and files under `/content` remain. Choose **Run all**. The install cell finishes quickly, files already downloaded from the Hugging Face Hub are normally reused, and the baseline and LoRA are trained again from the start with the same seed.
- **After a disconnect or runtime reset:** everything under `/content` is deleted, including checkpoints and exports. Start again with **Run all**, and download the archive (step 7) as soon as Section 11 finishes.
- The notebook does not continue an interrupted `Trainer` run from `travel_query_output/checkpoints/`. A rerun always trains all 3 epochs.

Outside Colab the notebook also runs locally (`pip install notebook` plus the packages above and PyTorch ≥ 2.5); outputs then go to `./travel_query_output/`, which `.gitignore` excludes.

## 6. Evaluation methodology

A model's score is `p_travel` = P(TRAVEL). The gate forwards a query when `p_travel >= threshold` and rejects it otherwise. TRAVEL is the positive class and "forward" counts as predicting TRAVEL:

| Metric | Definition |
| --- | --- |
| accuracy | correct routing decisions / all queries |
| travel precision | true travel / all forwarded |
| travel recall | forwarded travel / all travel |
| travel F1 | harmonic mean of travel precision and recall |
| travel false rejection rate | rejected travel / all travel (= 1 − recall), the most important error |
| non-travel rejection rate | rejected non-travel / all non-travel, the LLM calls avoided |
| share of queries rejected | all rejected / all queries |
| confusion matrix | `[[TN, FP], [FN, TP]]`; rows = true NOT_TRAVEL/TRAVEL, columns = rejected/forwarded |
| travel recall 95% CI | Wilson score interval |

Low travel precision is expected and acceptable here. CLINC150's validation and test sets are 97% NOT_TRAVEL, and the threshold deliberately forwards borderline queries. The non-travel rejection rate measures the savings; the false rejection rate measures the harm.

The notebook also measures median single-query latency on CPU after reloading the exported models.

## 7. Results

Two sets of results exist. **Section 7.1 is the record for the exported model:** the Colab T4 run that produced `travel_query_gate.zip`. Section 7.2 keeps the earlier local CPU runs made while preparing this folder. Their LoRA numbers come from a different training run and do not describe the exported adapter.

### 7.1 Colab T4 run (exported `travel_query_gate.zip`)

**Source:** `travel_query_gate/metadata.json` in the archive: `device: cuda (Tesla T4)`, Python 3.13.15, torch 2.11.0+cu130, transformers 5.19.0, peft 0.21.2, datasets 4.8.5, scikit-learn 1.9.1, joblib 1.6.0, numpy 2.1.3, pandas 2.2.3. The executed notebook (cell outputs) was not saved. The per-epoch training log, the restored epoch and peak GPU memory are therefore not available.

**Independent check (2026-10-21).** The check ran on macOS with an Apple M5 CPU and no GPU, using the same package versions (torch 2.11.0 CPU build). The archive was unzipped and both models were loaded with the snippet in Section 8. CLINC150 validation and test were then re-scored with the notebook's own preprocessing and metric code. Every metric and confusion matrix below matched `metadata.json` exactly. Re-deriving the LoRA threshold from the re-scored validation set gave 0.0287495 (saved: 0.0287494). No model was retrained and no threshold was changed.

**Training:** baseline 1.0 s; LoRA 108 s on the T4 (3 epochs).

**Validation** (80 TRAVEL / 2,996 NOT_TRAVEL; used for thresholds and model selection):

| Model | Threshold | Travel recall | False rejects | Non-travel rejection | Travel precision | Travel F1 | Accuracy | `[[TN, FP], [FN, TP]]` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Baseline | 0.1257 | 0.988 (79/80) | 1 | 0.933 | 0.281 | 0.438 | 0.934 | `[[2794, 202], [1, 79]]` |
| LoRA | 0.0287 | 0.988 (79/80) | 1 | 0.996 | 0.868 | 0.924 | 0.996 | `[[2984, 12], [1, 79]]` |

Both models reach equal validation recall, and LoRA rejects more non-travel queries, so the selection rule chose **LoRA**. Each model rejects the same validation travel query as in the CPU runs. The baseline rejects "do you think arizona has plenty of bike routes" (p = 0.037). LoRA rejects "what's the best restaurant in arizona for pizza" (p = 0.001).

**Test** (120 TRAVEL / 5,350 NOT_TRAVEL; scored after selection):

| Model | Travel recall (95% CI) | Travel false rejection rate | Non-travel rejection | Travel precision | Travel F1 | Accuracy | `[[TN, FP], [FN, TP]]` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Baseline | 1.000 (0.969–1.000) | 0.000 | 0.903 | 0.188 | 0.317 | 0.905 | `[[4832, 518], [0, 120]]` |
| LoRA (selected) | 0.958 (0.906–0.982) | 0.042 | 0.990 | 0.689 | 0.801 | 0.990 | `[[5298, 52], [5, 115]]` |

LoRA's test travel recall misses the 98% target, so the notebook's warning applies: do not enable automatic rejection with this model. Besides this run, the test set was also scored by the CPU runs in Section 7.2 and by the earlier Colab run that stopped at the reload check. No label policy, threshold or selection rule was changed in response.

**Reload check.** In Colab, the baseline had a maximum and mean score difference of 0.0 / 0.0 and LoRA 3.1e-6 / 4.7e-9, with no validation routing changes. The local check covered all 8,546 validation and test queries with two independent loads, a save-and-reload round trip and the notebook-style loader. All agreed within 1.2e-7, with no routing changes.

**Median single-query CPU latency** after reloading: Colab CPU baseline 1.5 ms, LoRA 59 ms; local Apple M5 CPU baseline 0.16 ms, LoRA 6.3 ms.

**Example routing** with the exported models (local check; hand-written queries, not a benchmark; `p` = P(TRAVEL)):

| Query | Baseline (threshold 0.1257) | LoRA (threshold 0.0287) |
| --- | --- | --- |
| Plan a trip to Japan | forward, uncertain (p = 0.18) | forward, uncertain (0.41) |
| Find hotels in Chicago | forward (0.98) | forward (1.00) |
| Help me plan a business trip | forward, uncertain (0.26) | **reject (0.026)** |
| Find cheap flights to New York | forward (0.85) | forward (1.00) |
| Suggest a vacation itinerary | forward, uncertain (0.26) | forward, uncertain (0.046) |
| What is Python? / How do I cook pasta? / Explain machine learning / What's the weather? | reject (≤ 0.05) | reject (< 0.0001) |
| Can you help me plan a vacation? | forward, uncertain (0.24) | forward (0.55) |
| I want to go somewhere warm in March for under $400 | forward (0.90) | forward, uncertain (0.44) |
| Weekend getaway ideas for college students on a budget | **reject (0.124)** | forward, uncertain (0.23) |
| Quiero reservar un vuelo a Madrid (Spanish: "I want to book a flight to Madrid"; ASCII only) | **reject (0.06)** | **reject (0.0001)** |

**Interpretation:**

- The Colab-trained LoRA still rejects "Help me plan a business trip" (p = 0.026 against a threshold of 0.0287), the same failure as in the CPU runs. Retraining on the GPU did not fix it. The 0.003 margin is about 1,000 times larger than any GPU/CPU or reload difference, so it is not a numerical artifact.
- LoRA blocks 5 of 120 test travel queries (4.2%). The baseline blocks none, but it forwards about 10% of non-travel queries to the LLM and also rejected one hand-written budget-travel query above.
- The non-ASCII rule forwards most non-English text. Spanish written without accents is ASCII, so it reaches the English-only model and is rejected.
- Neither model is ready for automatic rejection. Run the gate in shadow mode first (Section 9).

### 7.2 Earlier local CPU runs (not the exported model)

**These are from local CPU verification runs, not a Colab GPU run.** The notebook was executed top to bottom on 2026-10-19 while preparing this folder, on macOS with a 10-core CPU and no GPU. The only change was `TRAIN_LORA = True`, so LoRA trained in fp32 on CPU.

| Environment | torch | numpy / pandas | Outcome |
| --- | --- | --- | --- |
| Local packages (Python 3.13.0) | 2.14.1 | 2.5.4 / 3.0.6 | All cells passed in two runs, with identical thresholds and metrics (the tables below). |
| Colab's package versions (Python 3.13.0; see Section 5) | 2.11.0 | 2.1.3 / 2.2.3 | All cells passed. Same selected model and confusion matrices; LoRA metrics identical, baseline scores identical up to the 15th decimal place. |
| Colab GPU image versions with the dependency fix (Python 3.13.0; see Section 5) | 2.11.0 (CPU build) | 2.1.3 / 2.2.3 | All cells passed in two runs. Thresholds, selected model, confusion matrices, LoRA epoch F1 and example routings are identical to the tables below. |

The first two environments used transformers 5.19.0, peft 0.21.2, datasets 5.1.0 and scikit-learn 1.9.1. Neither contained `torchao` or Colab's RAPIDS packages, which is why neither showed the two errors that a real Colab GPU runtime produced (see *Troubleshooting* in Section 5). The third environment started from Colab's GPU-image versions of every package the notebook uses, including `torchao` 0.10.0, plus a metadata-only stand-in for `cudf-cu13` 26.6.0 that declares its PyArrow requirement. It reproduced both errors with the previous install cell. It then ran the corrected notebook with transformers 5.19.0, peft 0.21.2, datasets 4.8.5, scikit-learn 1.9.1 and no `torchao`. A Colab T4 run uses fp16 and GPU kernels, so its numbers may differ slightly. Treat your own run's `metadata.json` as the record. The reload-check fix (Section 5, *Troubleshooting*) does not change these CPU results: CPU runs train without mixed precision, so there is no autocast wrapper to remove.

**Protocol note.** An audit after the first verification run found texts shared between splits once punctuation is ignored, and the split-separation rule was tightened (Section 2). That first run had already scored the test set (LoRA recall 0.950, baseline 1.000), so this test set is no longer untouched for these two models. The label policy, thresholds and selection rule were not changed in response to test results.

**Training:** the baseline took 0.1 s. LoRA took about 200 s on CPU (3 epochs, 1,416 steps). Validation F1 at the 0.5 cut-off was 0.949 / 0.955 / 0.955 after epochs 1–3, so epoch 2 was restored as the best checkpoint.

**Validation** (80 TRAVEL / 2,996 NOT_TRAVEL; used for thresholds and model selection):

| Model | Threshold | Travel recall | False rejects | Non-travel rejection | Travel precision | Travel F1 | Accuracy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline | 0.126 | 0.988 (79/80) | 1 | 0.933 | 0.281 | 0.438 | 0.934 |
| LoRA | 0.064 | 0.988 (79/80) | 1 | 0.997 | 0.908 | 0.946 | 0.997 |

At equal validation recall, LoRA rejects far more non-travel queries, so the selection rule chose **LoRA**. Each model would reject one validation travel query: the baseline rejects "do you think arizona has plenty of bike routes" (p = 0.04), and LoRA rejects "what's the best restaurant in arizona for pizza" (p = 0.001), a `travel_suggestion` example that reads like a restaurant question.

**Test** (120 TRAVEL / 5,350 NOT_TRAVEL; scored after selection):

| Model | Travel recall (95% CI) | Travel false rejection rate | Non-travel rejection | Travel precision | Travel F1 | Accuracy | `[[TN, FP], [FN, TP]]` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Baseline | 1.000 (0.969–1.000) | 0.000 | 0.903 | 0.188 | 0.317 | 0.905 | `[[4832, 518], [0, 120]]` |
| LoRA (selected) | 0.958 (0.906–0.982) | 0.042 | 0.993 | 0.747 | 0.839 | 0.992 | `[[5311, 39], [5, 115]]` |

**Example routing** (hand-written queries, not a benchmark; `p` = P(TRAVEL)):

| Query | Baseline | LoRA |
| --- | --- | --- |
| Plan a trip to Japan | forward, uncertain (p = 0.18) | forward, uncertain (0.35) |
| Find hotels in Chicago | forward (0.98) | forward (1.00) |
| Help me plan a business trip | forward, uncertain (0.26) | **reject (0.01)** |
| What is Python? / How do I cook pasta? / What is the weather? | reject (≤ 0.03) | reject (< 0.0001) |
| I want to go somewhere warm in March for under $400 | forward (0.90) | forward, uncertain (0.19) |
| Can you help me plan a vacation? | forward, uncertain (0.24) | forward, uncertain (0.47) |
| Do I need a visa for Japan? | reject (0.03) | reject (0.0001) |
| What's the weather in Paris during my trip? | reject (0.12) | reject (0.005) |

**Median single-query CPU latency** after reloading from disk: baseline 0.17 ms, LoRA 6.7 ms.

**Interpretation of the CPU runs:**

- LoRA's validation threshold did not transfer. Its test travel recall (95.8%, 5 of 120 travel queries blocked) misses the 98% target, and the notebook printed its warning. With 80 validation positives, a 98% target allows one validation miss, so the threshold of a very confident model is set by a single example.
- LoRA also rejects one of the required TRAVEL examples ("Help me plan a business trip", p = 0.01). It forwards "Plan a trip to Japan" and the $400 warm-weather request only through the uncertain band. This is consistent with the phrasing gaps described in Section 2.
- The baseline blocked no test travel queries and routed all six requirement examples correctly. In exchange, it forwards about 10% of non-travel queries to the LLM.
- **Neither model should reject real users automatically yet.** On this evidence the baseline is the safer gate, but that conclusion uses test results. Under the protocol it should be confirmed on new, app-specific labeled data, not by editing the selection rule and re-reading this test set.

## 8. Saved model and local inference

Section 11 exports:

```text
travel_query_gate/
├── metadata.json            # selected model, thresholds, label policy, revisions, versions, measured metrics
├── baseline.joblib          # TF-IDF + Logistic Regression pipeline (~360 KB)
└── lora_adapter/            # only if LoRA was trained
    ├── adapter_config.json
    ├── adapter_model.safetensors   # LoRA matrices + trained pre_classifier/classifier (~2.8 MB)
    ├── README.md                   # model card generated by PEFT
    ├── tokenizer.json
    └── tokenizer_config.json
```

`travel_query_gate.zip` contains the same folder (about 3 MB with LoRA). Keep these files out of Git. In this folder, `.gitignore` excludes `travel_query_output/`, `content/` and an unzipped `travel_query_gate/`, plus any `*.zip`, `*.safetensors` or `*.joblib` file. Store them as a release asset or on shared storage.

The notebook reloads these files on CPU and asserts that they reproduce the in-memory float32 validation scores: every score must be within 1e-3 and no validation routing decision may change. `metadata.json` records the maximum and mean absolute score difference and the number of routing changes under `reload_check`. The LoRA adapter is not a standalone model; loading it also downloads the pinned DistilBERT base weights (about 270 MB) once. `baseline.joblib` is a pickle: load only files you created, and load them with the same scikit-learn version (`1.9.1`).

Loading the export outside the notebook (install `scikit-learn==1.9.1 joblib==1.6.0`, plus `torch transformers==5.19.0 peft==0.21.2` for LoRA; PEFT refuses to load the adapter if a `torchao` older than 0.16.0 is installed, so uninstall it, as described in Section 5):

```python
import json
from pathlib import Path

import joblib

EXPORT = Path("travel_query_gate")  # unzipped travel_query_gate.zip
meta = json.loads((EXPORT / "metadata.json").read_text())
name = meta["selected_model"]
threshold = meta["thresholds"][name]

if name == "baseline":
    model = joblib.load(EXPORT / "baseline.joblib")  # only load files you created
    score_fn = lambda texts: model.predict_proba(list(texts))[:, 1]
else:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(EXPORT / "lora_adapter")
    base = AutoModelForSequenceClassification.from_pretrained(
        meta["base_model"]["id"], revision=meta["base_model"]["revision"], num_labels=2)
    model = PeftModel.from_pretrained(base, EXPORT / "lora_adapter").eval()

    @torch.inference_mode()
    def score_fn(texts):
        batch = tok(list(texts), truncation=True, max_length=meta["max_length"],
                    padding=True, return_tensors="pt")
        return torch.softmax(model(**batch).logits, dim=-1)[:, 1].numpy()

# classify_query: copy it unchanged from notebook Section 10.
print(classify_query("Find hotels in Chicago", score_fn, threshold))
```

**Prediction interface** (`classify_query(text, score_fn, reject_threshold, active_travel_context=False)`):

```python
{"label": "TRAVEL" | "NOT_TRAVEL" | None,   # None when the model was not consulted
 "travel_probability": float | None,        # P(TRAVEL), uncalibrated
 "confidence": float | None,                # probability of `label`
 "route": "forward" | "reject",             # forward = send to extract
 "reason": "travel" | "uncertain" | "not_travel"
           | "active_travel_context" | "language_not_validated" | "empty_input"}
```

Rules are applied in this order:

1. An active travel conversation is always forwarded.
2. Empty input is rejected.
3. Text containing non-ASCII characters is forwarded, because the model has seen only English. Typographic quotes from phone keyboards are normalized first.
4. Otherwise: `p ≥ 0.5` → forward (`travel`); `threshold ≤ p < 0.5` → forward (`uncertain`); `p < threshold` → reject (`not_travel`).

## 9. Proposed integration before the `extract` node (not implemented)

In `build_graph()` only the **first** message of a conversation enters at `START → extract`. Replies to follow-up questions resume at `wait_for_reply → extract`. The gate therefore belongs on the `START` edge, and follow-ups such as "yes", "2 people" or "What about tomorrow?" never pass through it. A sketch:

```python
# backend/services/agent/graph.py (proposal only)
def gate(state: TripState) -> dict:
    text = state["messages"][-1].content
    active = any(v is not None for v in state.get("constraints", {}).values())
    try:
        decision = classify_query(text, score_fn, threshold, active_travel_context=active)
    except Exception:
        decision = {"route": "forward", "reason": "gate_error"}  # fail open: never block on errors
    return {"gate": decision}

def out_of_scope(state: TripState) -> dict:
    return {"messages": [AIMessage(content="I can help you plan trips: flights, hotels, "
                                           "rental cars and things to do. Where would you like to go?")]}

builder.add_node("gate", gate)
builder.add_node("out_of_scope", out_of_scope)
builder.add_edge(START, "gate")  # replaces START -> extract
builder.add_conditional_edges("gate", lambda s: s["gate"]["route"],
                              {"forward": "extract", "reject": "out_of_scope"})
builder.add_edge("out_of_scope", END)
```

Integration notes:

- Add `gate: NotRequired[dict]` to `TripState` so the decision is kept in graph state for logging and shadow mode. Without it, routing still works in langgraph 1.2.8, but the decision is dropped from the state.
- This sketch was exercised in a scratch test, without editing `backend/`. It copied `build_graph()` in memory, swapped the `START → extract` edge for the lines above, mocked the LLM and used a real baseline export. Non-travel first messages ended at `out_of_scope` with zero LLM calls. Travel and uncertain ones reached `extract`. A follow-up reply skipped the gate, and a scoring error failed open. The test was repeated on 2026-10-21 with both models from the Colab archive, with the same results. The model was loaded once for all requests. A reused thread with trip constraints was forwarded as `active_travel_context`. The LoRA gate sent "Help me plan a business trip" to `out_of_scope` (Section 7.1).
- Load the model once at process start, not per request.
- Pass `active_travel_context=True` whenever a conversation already has trip constraints, for example if a future chat route reuses a thread.
- `backend/requirements.txt` would need `scikit-learn` and `joblib` (baseline) or `torch`, `transformers` and `peft` (LoRA).
- `backend/tests/test_trip_loop.py` invokes the graph from `START`, so its tests would need to mock or bypass the gate.
- Roll out in **shadow mode** first: log `route` and `reason` but always forward. Then measure false rejects and avoided LLM calls on real traffic before enabling rejection.

## 10. Limitations and next steps

**Limitations:**

- Proxy data: see Section 2. The gate has not been evaluated on real TravelAgentic messages.
- English only and single-turn. Context comes only from the `active_travel_context` flag.
- Scores are not calibrated probabilities, and a threshold is valid only for the model it was chosen with.
- GPU training (fp16 on a T4) ran once in Colab with the corrected notebook and produced the exported archive (Section 7.1). Its reload check passed. The executed notebook was not saved, so its cell outputs are not available: the per-epoch training log, the restored epoch and peak GPU memory. Only one seed was run.
- The archive does not contain the base DistilBERT weights (about 270 MB). The first load downloads them from the Hugging Face Hub at the pinned revision, so an offline backend needs them cached or vendored.
- Non-English text without accents (for example Spanish) is ASCII, so the non-ASCII rule does not catch it, and the English-only model scores it (Section 7.1).
- Latency was measured on one CPU, not in the deployment environment.

**Next steps:**

1. Label a few hundred real first messages (including hard negatives, weather or visa questions, and non-English text) as an independent test set.
2. Run the gate in shadow mode and compare its decisions with those labels.
3. Retrain or re-threshold with app-specific training data.
4. Only then enable automatic rejection, and keep monitoring false rejects.

## 11. References

- Larson et al. (2019), *An Evaluation Dataset for Intent Classification and Out-of-Scope Prediction*, EMNLP. Dataset: <https://huggingface.co/datasets/clinc/clinc_oos>
- Sanh et al. (2019), *DistilBERT, a distilled version of BERT*. Model: <https://huggingface.co/distilbert/distilbert-base-uncased>
- Hu et al. (2021), *LoRA: Low-Rank Adaptation of Large Language Models*. PEFT docs: <https://huggingface.co/docs/peft>
- Hugging Face `Trainer`: <https://huggingface.co/docs/transformers/main_classes/trainer>
