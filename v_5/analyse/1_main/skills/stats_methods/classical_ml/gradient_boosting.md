# Gradient boosting (XGBoost, HistGradientBoosting)

Apply the main classical_ml guidance first.

## Fit

- **XGBoost:** `xgboost.XGBClassifier` or `XGBRegressor(n_estimators=2000, learning_rate=0.05, max_depth=4, subsample=0.8, colsample_bytree=0.8, min_child_weight=1, reg_lambda=1.0, tree_method='hist', n_jobs=1, early_stopping_rounds=50, eval_metric='logloss' or 'rmse', random_state=0)`. Fit with `eval_set=[(X_val, y_val)]`, where the validation set is split from the training data, never the test set.
- **HistGradientBoosting:** `HistGradientBoostingClassifier` or `Regressor(learning_rate=0.05, max_iter=1000, early_stopping=True, validation_fraction=0.15, random_state=0)`. This scikit-learn alternative works directly inside pipelines; `categorical_features='from_dtype'` handles pandas categoricals.
- Both handle missing values natively. Do not impute for them unless comparing.
- Small tuning grid: `max_depth` ∈ {3, 4, 6} and `learning_rate` ∈ {0.03, 0.1}, with early stopping choosing the number of trees.
- For imbalance, keep the natural balance and tune the threshold. `scale_pos_weight` distorts probabilities; if used, report calibration.
- Weights: pass `sample_weight=w` to `fit`, and `sample_weight_eval_set` for the validation set.

## Checks specific to boosting

- **Early-stopping round:** if the best iteration is at the cap, raise `n_estimators`. If it is very early (under about 50), the signal is weak or the learning rate is too high.
- **Overfitting:** boosting overfits more readily than forests. Compare the cross-validated and training scores, and prefer shallower trees when the gap is large.

## Interpret

- Rank predictors by permutation importance on the test set. Do not use XGBoost's default `feature_importances_` (gain), which is biased like impurity importance.
- Per-prediction contributions (SHAP values) come built in: `model.get_booster().predict(xgboost.DMatrix(X_test), pred_contribs=True)`. Average the absolute values per predictor for a global ranking, and use their sign for direction. Explain them as "how much each characteristic pushed this prediction up or down from the average".
- Wording: "The boosted model predicts annual energy spending with a typical error of ±180 units (RMSE), compared with ±260 for a model that predicts the average. Floor area and heating fuel matter most."

## Headline model

- `ŷᵢ = Σ_{m=1}^{M} η·f_m(xᵢ)`, where each tree f_m is fitted to the gradient of the loss from the previous rounds. XGBoost minimises `Σᵢ l(yᵢ, ŷᵢ) + Σ_m [γT_m + ½λ‖w_m‖²]`. Notation: η is the learning rate, T_m the number of leaves and w_m the leaf weights of tree m (Chen & Guestrin, 2016).

References: [XGBoost parameters](https://xgboost.readthedocs.io/en/stable/parameter.html) · [Chen & Guestrin 2016](https://doi.org/10.1145/2939672.2939785) · [scikit-learn HistGradientBoosting](https://scikit-learn.org/stable/modules/ensemble.html#histogram-based-gradient-boosting)
