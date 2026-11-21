# Support vector machines

Apply the main classical_ml guidance first.

## When it fits

Use it for medium-sized numeric data (up to about 20,000 rows in the sandbox, since training time grows faster than linearly) with complex class boundaries. Trees are usually as good on mixed tabular data and easier to explain, so compare against random forest.

## Fit

- `Pipeline([('scale', StandardScaler()), ('model', SVC(kernel='rbf', C=C, gamma='scale', class_weight=None, probability=False))])`. For regression, use `SVR`.
- Tune `C` ∈ {0.1, 1, 10} and `gamma` ∈ {'scale', 0.01, 0.1} by cross-validation.
- Scaling is mandatory. One-hot encode categorical predictors.
- For probabilities, use `CalibratedClassifierCV(svc, method='sigmoid', cv=5)` rather than `probability=True`, and report the Brier score.
- For more than about 20,000 rows, use `LinearSVC` (a linear boundary) or a subsample.
- Weights: `fit(X, y, model__sample_weight=w)`.

## Interpret

- An RBF SVM has no readable coefficients. Use permutation importance and partial dependence.
- Wording: report performance as in the main guidance. Say the model "learns a flexible boundary between the groups".

## Headline objective

- `min_{w,b} ½‖w‖² + C Σᵢ max(0, 1 − yᵢ f(xᵢ))`, with the kernel `K(x, x′) = exp(−γ‖x − x′‖²)`. Notation: C trades margin width against training errors, and γ sets how local the boundary is.

References: [scikit-learn SVM](https://scikit-learn.org/stable/modules/svm.html) · [scikit-learn probability calibration](https://scikit-learn.org/stable/modules/calibration.html)
