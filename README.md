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
| Metropolis-Hastings | isotropic random walk | **did not converge** in 10,000 draws |
| Adaptive MH | proposal covariance learned from the chain | **failed**, 1.2% acceptance |
| Preconditioned MH | proposal shaped by the observed Fisher information | works, ESS 217 |
| Gibbs | exact full conditionals from conjugacy | best ESS per second |
| HMC | gradient-informed, leapfrog integrator | ESS 7,997 |
| Student-t Gibbs | robust likelihood as a normal scale mixture | fixes the calibration |

## Main findings

**Plain Metropolis-Hastings never converged, and a weak diagnostic hid it.** Under the plain
Gelman-Rubin statistic every sampler scored 1.03 or below, which reads as convergence. Split R-hat
puts MH at 1.47, and its posterior means are wrong by up to 0.315 — larger than the standard
deviation of the widest direction of the posterior (0.044).

**The cause is geometric, not generic.** The posterior covariance has condition number 273 and a
maximum coefficient correlation of 0.77, because the lagged CPU features are nearly collinear. A
single isotropic step size has to fit a direction of width 0.0027 while crossing one of width
0.044.

**Textbook adaptive Metropolis fails here, and the reason is instructive.** Learning the proposal
covariance from the chain's own history cannot work when the chain has an autocorrelation time of
~1,000 and only 2,000 burn-in iterations: it measures the transient drift toward the mode rather
than the posterior. Supplying the metric externally, from the observed Fisher information, raises
ESS from 9.3 to 217 and brings acceptance to 27.5% against the theoretical optimum of 23.4%.

**The over-wide credible intervals were blamed on the priors; they are caused by the data.** The
test residuals have an excess kurtosis of 108.9. A plain OLS fit, with no priors and no MCMC,
reproduces the same over-coverage. A Student-t likelihood sampled as a normal scale mixture moves
50% interval coverage from 94.5% to 73.7% against a nominal 50%, narrows the mean 50% interval
from 0.534 to 0.068, and cuts the median absolute error from 0.046 to 0.019.

**Validation.** Gibbs, HMC and preconditioned MH agree on the posterior means to within 0.0006.
The same posterior sampled with [emcee](https://emcee.readthedocs.io), a third-party library using
an unrelated ensemble algorithm, agrees to within 0.0008.

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
python src/run_experiment_v2.py --skip-nuts   # ~1 minute -> results/ JSON + figures
python src/create_report_v2.py                # -> docs/Sampling_Project_Report.docx
```

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
