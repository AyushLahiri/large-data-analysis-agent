# Classical machine learning

Use this when the user wants to predict or classify an outcome from many characteristics, find which characteristics predict it best, or capture non-linear patterns and interactions that regression would miss. Linear, logistic, lasso, ridge and elastic net models are in **stats_methods/regression/regression**. Then read one reference:

| Situation | Reference |
|---|---|
| Strong general-purpose default for tabular data; mixed variable types; robust with little tuning | **stats_methods/classical_ml/random_forest** |
| Best accuracy on tabular data, willing to tune | **stats_methods/classical_ml/gradient_boosting** (XGBoost, HistGradientBoosting) |
| A model whose rules must be read and explained | **stats_methods/classical_ml/decision_tree** |
| Small numeric data where similar cases should have similar outcomes | **stats_methods/classical_ml/knn** |
| Medium-sized numeric data with complex boundaries | **stats_methods/classical_ml/svm** |
| Very small samples or many categorical predictors; a fast baseline | **stats_methods/classical_ml/naive_bayes** |
| Many numeric predictors with smooth non-linear patterns, when tree models underperform | **stats_methods/classical_ml/neural_net** |

Python has `scikit-learn`, `xgboost`, `numpy` and `pandas`. For finding groups without an outcome, use **stats_methods/clustering/clustering**.

## Plan

- Define the outcome, the prediction population, and whether it is classification (categories) or regression (a number). Choose predictors that would be known at prediction time. Exclude anything that is a consequence, duplicate or recode of the outcome; this is leakage (Kaufman et al., 2012).
- When possible, also run two simpler comparison models (see "Tuning and comparing models"). Skip either one when the data make it meaningless.
- Run the model and flag weak performance. Skip only when fitting is impossible: one outcome class, or fewer than about 50 rows. In that case, say it and use a regression model.

## Prepare the data

- **Split first, before any fitting or preprocessing:** hold out a stratified 20–25% test set with `train_test_split(..., stratify=y, random_state=0)`. For n below about 1,000, use repeated stratified 5-fold cross-validation for evaluation instead of a single split.
- **Avoid leakage:** put every learned preprocessing step (scaling, imputation, encoding, selection) inside a `Pipeline` with `ColumnTransformer`, so it is fitted on training folds only.
- **Tune hyperparameters** following "Tuning and comparing models" below.
- **Missing data:** drop rows missing the outcome. For predictors, either use complete cases (report how many rows were dropped) or impute inside the pipeline (`SimpleImputer`, adding `add_indicator=True` when missingness may carry information). HistGradientBoosting and XGBoost handle missing values natively.
- **Categorical predictors:** map to labels, then `OneHotEncoder(handle_unknown='ignore', min_frequency=…)` to group rare levels. Use ordinal encoding only for truly ordered categories or for tree models.
- **Scaling:** needed for kNN, SVM and neural nets (`StandardScaler`). Not needed for tree models.
- **Class imbalance:** keep the natural class balance by default and judge with balanced metrics. Oversampling or undersampling distorts predicted probabilities (van den Goorbergh et al., 2022). If the user needs a decision rule, tune the probability threshold on cross-validated predictions instead (`TunedThresholdClassifierCV`, or by hand), and report the threshold.
- **Weights:** most estimators accept `sample_weight` in `fit` (in a pipeline: `model__sample_weight=w`). kNN and the MLP do not. With weights, also compute test metrics with `sample_weight`, and say so.
- **Sandbox limits:** 1 CPU, 1 GB memory and 240 seconds per script. Keep grids small (12 or fewer combinations × 5 folds), use `n_jobs=1`, and subsample above about 100,000 rows for tuning.

## Tuning and comparing models

- **Comparison models (when possible, not required):**
  - **Regression baseline:** a logistic or linear model from stats_methods/regression on the same predictors. It shows whether the ML model's non-linearity adds anything.
  - **Naive rule:** the simplest sensible rule, either predicting the majority class or mean (`DummyClassifier(strategy='prior')`, `DummyRegressor()`), or a one-variable rule using the single best predictor (a depth-1 decision tree; Holte, 1993). It shows how much any model learns beyond the obvious.
  - Skip one when it is meaningless for the data, for example a regression baseline with more predictors than rows, or a one-variable rule when no single predictor is defensible. Say which comparisons ran.
- **Same splits:** every compared model uses the same train/test split and the same CV folds. Create the splitter once (`StratifiedKFold(5, shuffle=True, random_state=0)`, or `RepeatedStratifiedKFold(n_splits=5, n_repeats=3, random_state=0)`) and pass the same object to every `GridSearchCV` and `cross_val_score`. Differences then reflect the models, not the data each one saw.
- **Tuning:** use cross-validation on the training data only (`GridSearchCV` or `RandomizedSearchCV`), with the grids in each sub-method guide. Report test-set performance once, at the end. Never tune on the test set; that reuse makes performance look better than it is (Varma & Simon, 2006).
  - Below about 1,000 rows, where there is no separate test set, use nested cross-validation: tune in an inner loop and score in the outer folds (`cross_val_score(GridSearchCV(...), X, y, cv=outer)`).
- **Is the difference real?** Compare per-fold scores on the shared folds. If the mean difference between two models is smaller than the fold-to-fold standard deviation of that difference, treat them as tied. For a formal test on repeated CV, use the corrected resampled t-test (Nadeau & Bengio, 2003), whose variance term is multiplied by (1/k·r + n_test/n_train) rather than 1/k·r.
- **Ties:** prefer the simpler, more interpretable model, for example the regression over the forest, and say they performed similarly.
- **Report the tuning outcome** (the chosen values) in the methodology only, not in the answer text.

## Checks

| Check | Action |
|---|---|
| Leakage | If test performance is near perfect, look for an outcome proxy among the predictors before reporting |
| Overfitting | Compare training and cross-validated scores. A large gap means the model memorises; simplify or regularise |
| Comparison models | If the ML model does not beat the regression baseline or naive rule beyond fold noise, say the extra complexity adds nothing |
| Class sizes | The smallest class needs enough test cases for stable metrics. Report its count, and flag fewer than about 50 test cases in it |
| Calibration (when probabilities are reported) | Brier score and a binned calibration table (`calibration_curve`, 5–10 bins). Poor calibration means the probabilities should not be read literally (Van Calster et al., 2019) |
| Stability | Repeat the split or CV with 3–5 seeds. Report the spread of the main metric |

## Interpret

- The headline is held-out performance in plain terms, then what drives predictions.
- **Variable importance:** use permutation importance on the test set (`sklearn.inspection.permutation_importance`, `n_repeats=10`). Impurity-based importance favours variables with many categories or values (Strobl et al., 2007). Correlated predictors share importance, so a low score does not mean irrelevant.
- **Direction and shape:** use partial dependence (`sklearn.inspection.partial_dependence`) for the top 2–3 predictors, and describe the pattern in words, for example "risk rises steeply until age 40, then flattens".
- **Prediction is not causation:** important predictors are useful for prediction, not proven causes. Say "most predictive of", never "drives" or "causes".

## Key metrics to report

**Calculate all, report three:** compute every metric listed for the method. In the answer and tables, report at most three that together give the main overview of the result for this question. Mention any other metric only when it shows a problem, as one plain flag sentence, for example "The model's probabilities run too high for the riskiest group (poor calibration)."

**Classification** (all on the held-out test set or cross-validated predictions):

| Metric | Report | Plain meaning |
|---|---|---|
| Confusion matrix (counts) | Always | Correct and incorrect predictions per class |
| Balanced accuracy | Always | Average of the per-class hit rates; not inflated by a dominant class |
| Sensitivity / recall (true positive rate) | Binary, or per class | Share of actual positives correctly found |
| Specificity (true negative rate) and false positive rate = 1 − specificity | Binary | Share of actual negatives correctly cleared, and those wrongly flagged |
| Precision (positive predictive value) | Binary, or per class | When the model says "yes", how often it is right |
| F1 | When precision and recall both matter | Balance of precision and recall |
| ROC AUC | Binary with probabilities | Chance a random positive scores higher than a random negative; 0.5 is guessing |
| Precision-recall AUC (average precision) | Binary with positives below about 20% | More informative than ROC under imbalance (Saito & Rehmsmeier, 2015); compare with the positive rate |
| Brier score | When probabilities are reported | Mean squared error of the probabilities; lower is better; compare with the baseline |
| Accuracy | Only with balanced classes, and alongside the above | Misleading under imbalance |

State the decision threshold, 0.5 unless tuned. Rates depend on it.

**Regression:** RMSE and MAE in the outcome's units, test-set R² (out-of-sample, which can be negative), and the baseline RMSE. Adjusted R² applies to in-sample linear models (see regression), not to ML test performance.

**Always:** n for training and test, number of predictors after encoding, the CV scheme, and the spread across seeds or folds.

## Present

- **Table 1, performance:** one row per model that ran (naive rule, regression baseline, ML model) and at most three headline metric columns on the test set, plus n.
- **Table 2, drivers:** the top 10 predictors by permutation importance with the mean drop in score and its spread.
- **Table 3, optional:** the confusion matrix for classification.
- **Words:** two to four sentences:
  1. How well it predicts, in plain terms. Example: "The model correctly identifies 78% of households that save energy and 71% of those that do not (balanced accuracy 0.75), clearly better than guessing (0.50) and slightly better than logistic regression (0.72)."
  2. The two or three strongest predictors and their direction.
  3. One caveat only if it changes how the result is read.

  Do not narrate tuning grids, packages or checks that passed.

## Methodology (method.steps)

Use short bold labels in pipeline order and stay within eight bullets:

- **Sample:** population, n, rows dropped, and the outcome definition and class balance.
- **Predictors:** the count and groups, plus any exclusions for leakage.
- **Preprocessing:** encoding, scaling, imputation and the pipeline.
- **Validation:** the split or CV scheme, the seeds, and the tuning search with its chosen values.
- **Model:** the algorithm and its headline objective from the reference.
- **Notation:** one line defining the symbols.
- **Evaluation:** the metrics, the threshold, and the weighted or unweighted basis.
- **Interpretation:** permutation importance and partial dependence settings.

References: [scikit-learn model evaluation](https://scikit-learn.org/stable/modules/model_evaluation.html) · [scikit-learn common pitfalls (leakage)](https://scikit-learn.org/stable/common_pitfalls.html) · [TRIPOD+AI 2024](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11025451/) · [Saito & Rehmsmeier 2015](https://pmc.ncbi.nlm.nih.gov/articles/PMC4349800/) · [Strobl et al. 2007](https://pmc.ncbi.nlm.nih.gov/articles/PMC1796903/) · [Varma & Simon 2006](https://doi.org/10.1186/1471-2105-7-91) · [van den Goorbergh et al. 2022](https://doi.org/10.1093/jamia/ocac093) · [Van Calster et al. 2019](https://doi.org/10.1186/s12916-019-1466-7) · [Kaufman et al. 2012, leakage](https://doi.org/10.1145/2382577.2382579) · [Nadeau & Bengio 2003, corrected resampled t-test](https://doi.org/10.1023/A:1024068626366) · [Holte 1993, one-rule classifiers](https://doi.org/10.1023/A:1022631118932)
