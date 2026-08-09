# MCMC Sampling Methods for Bayesian Linear Regression

Predicting cloud server CPU load using the Bitbrains datacenter traces.

**Elad Dagmi & Shaked Mizrahi** — Advanced Methods in Machine Learning, August 2026

Report: [`docs/Sampling_Project_Report.docx`](docs/Sampling_Project_Report.docx)

---

## What this project does

It compares MCMC samplers for Bayesian linear regression on a real prediction task: forecasting
the CPU utilisation of virtual machines in a production datacenter. A point forecast is of limited
operational use; what capacity planning needs is a calibrated statement of how uncertain that
forecast is, which is what the Bayesian treatment provides.

Five samplers are implemented from scratch in NumPy and SciPy, with no probabilistic programming
library used inside any of them:

| Sampler | Idea | Result on this posterior |
|---|---|---|
| Metropolis-Hastings | isotropic random walk | **did not converge**, R-hat 4.40 |
| Adaptive MH | proposal covariance learned from the chain | **did not converge**, 1.1% acceptance |
| Preconditioned MH | proposal shaped by the observed Fisher information | converged, bulk ESS 627 |
| Gibbs | exact full conditionals | converged, best ESS per second |
| HMC | gradient-informed, leapfrog integrator | converged, bulk ESS 15,905 |
| Student-t Gibbs | robust likelihood as a normal scale mixture | improves the calibration substantially |

## Main findings

Every number below is read from `results/experiment_results_v2.json` produced by the current run
and matches the shipped report. The earlier round of experiments, and the corrections that led
here, are preserved in [`docs/PROJECT_STATUS_AND_FINDINGS.md`](docs/PROJECT_STATUS_AND_FINDINGS.md)
and [`docs/archive/`](docs/archive/).

**Two of the five samplers never converged.** With 4 overdispersed chains and
rank-normalised, folded R-hat, plain Metropolis-Hastings reaches R-hat 4.40 and adaptive
Metropolis 3.35, against a threshold of 1.01. Their posterior means are wrong by
1.36 and 1.44 against a deterministic quadrature reference — larger than the standard
deviation of the widest direction of the posterior (0.050), so the errors exceed the entire
posterior spread.

**The cause is geometric, not generic.** The posterior covariance has condition number
314 and a maximum coefficient correlation of 0.69, because the lagged CPU features
are nearly collinear. A single isotropic step size has to fit a direction of width 0.0028
while crossing one of width 0.0499.

**Textbook adaptive Metropolis fails under the configuration tested, and the reason is
instructive.** Learning the proposal covariance from the chain's own history cannot work when the
chain barely moves during the 2,000 burn-in iterations available: it measures the transient
drift toward the mode rather than the posterior, and acceptance collapses to 1.1%. Supplying the
metric externally, from the observed Fisher information, raises bulk ESS from 4.2 to
627 and brings acceptance to 24.6% against the theoretical optimum of 23.4%.

**The over-wide predictive intervals are caused by the data, not the priors.** The test residuals
have an excess kurtosis of 111. A plain OLS fit, with no priors and no MCMC, reproduces the
same over-coverage. A Student-t likelihood sampled as a normal scale mixture, with nu selected on
validation data, moves nominal 50% interval coverage from 94.7% to 74.1% and cuts the median
absolute error from 0.210 to 0.072 CPU percentage points. That is a substantial
improvement rather than a fix: at 74.1% against a nominal 50% the intervals remain clearly
miscalibrated, and the report says so.

**Validation.** Gibbs, HMC and preconditioned MH agree with a deterministic quadrature reference,
which involves no sampling at all, to within 0.0025. The same posterior sampled with
[emcee](https://emcee.readthedocs.io), a third-party library using an unrelated ensemble
algorithm, agrees to within 0.00112. The Student-t model is diagnosed on the same footing:
4 chains, R-hat 1.0012, minimum bulk ESS 2272.

## Repository layout

```
mcmc-sampling-project/
├── src/                  experiment and document generators
├── notebooks/            annotated notebook for the first round
├── results/              numerical results and every figure
│   └── figures/
├── docs/                 the report, the proposal deck and its script
│   └── archive/          superseded documents, kept for provenance
├── data/                 input traces (not committed — see data/README.md)
└── requirements.txt
```

| Path | Contents |
|---|---|
| [`src/run_experiment_v2.py`](src/run_experiment_v2.py) | Current experiment script: all five samplers, corrected diagnostics, residual and posterior-geometry analysis, Student-t sampler, initialisation study, emcee reference, all figures |
| [`src/create_report_v2.py`](src/create_report_v2.py) | Generates `docs/Sampling_Project_Report.docx` from the results JSON |
| [`src/run_experiment.py`](src/run_experiment.py), [`src/create_report.py`](src/create_report.py) | First-round scripts, kept for provenance |
| [`src/create_proposal.py`](src/create_proposal.py), [`src/create_script.py`](src/create_script.py) | Generate the proposal deck and its presenter script |
| [`results/experiment_results_v2.json`](results/experiment_results_v2.json) | Numerical results reported in the document |
| [`results/experiment_results.json`](results/experiment_results.json) | First-round results, cited where the report compares against them |
| [`results/figures/`](results/figures/) | Every figure embedded in the report |
| [`notebooks/Sampling_Project.ipynb`](notebooks/Sampling_Project.ipynb) | Annotated notebook for the first round |
| [`docs/PROJECT_STATUS_AND_FINDINGS.md`](docs/PROJECT_STATUS_AND_FINDINGS.md) | What changed between the two rounds, and why |
| [`docs/archive/`](docs/archive/) | The report as it stood before the corrections |

## Reproducing the results

```bash
pip install -r requirements.txt

# download the data first (see data/README.md), then, from the repository root:
python src/run_experiment_v2.py --skip-nuts   # ~18 minutes -> results/ JSON + figures
python src/create_report_v2.py                # a few seconds -> docs/Sampling_Project_Report.docx
```

The experiment took about 18 minutes on the machine used for the current results. Most of that is
Hamiltonian Monte Carlo, which is run for 4 chains and 3 independent repeats, and the emcee
reference; the figure varies with machine load.

Every number and figure in the report is produced by these two commands. Nothing is hand-copied.
The scripts resolve their own paths, so they can be run from any working directory.

`--skip-nuts` omits an optional second external reference via PyMC. It is not needed: the external
validation in Section 5.4 uses emcee, which is pure Python. PyMC compiles its log-posterior through
PyTensor and is impractically slow without a C++ compiler installed.

One caveat on exact reproducibility: every sampler written here is seeded and reproduces to the
last digit, but emcee draws from the global NumPy random state, so the external-reference deviation
in Section 5.4 moves by roughly 0.0002 between runs.

## Data

The traces are **not committed here** — the `fastStorage` directory is 1.19 GB across 1,250 CSV
files, and the dataset is already published by its authors. See
[`data/README.md`](data/README.md) for the download link and the expected layout.

Citation: S. Shen, V. van Beek, and A. Iosup, "Statistical Characterization of Business-Critical
Workloads Hosted in Cloud Datacenters," *CCGrid* 2015.
