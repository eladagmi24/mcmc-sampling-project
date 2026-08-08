"""Experiments for the MCMC sampling study.

Pipeline: load the Bitbrains traces, build a one-step-ahead forecasting design, split it
chronologically into train / validation / test, run five samplers against the Gaussian posterior
and a Student-t model selected on validation, diagnose convergence, validate against an analytical
reference and an external library, and write every number to experiment_results_v2.json.

Run with --skip-nuts to omit the optional PyMC cross-check, which is impractically slow on a
machine without a C++ compiler. Nothing else depends on it.
"""
import glob
import json
import os
import sys
import time

import numpy as np
import pandas as pd

os.environ.setdefault('PYTENSOR_FLAGS', 'cxx=')
sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

SOURCE_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
REPOSITORY_ROOT = os.path.dirname(SOURCE_DIRECTORY)     if os.path.isdir(os.path.join(os.path.dirname(SOURCE_DIRECTORY), 'data')) else SOURCE_DIRECTORY
DATA_DIRECTORY = os.path.join(REPOSITORY_ROOT, 'data', 'fastStorage', '2013-8')
RESULTS_DIRECTORY = os.path.join(REPOSITORY_ROOT, 'results')
FIGURE_DIRECTORY = os.path.join(RESULTS_DIRECTORY, 'figures')
RESULTS_FILE = os.path.join(RESULTS_DIRECTORY, 'experiment_results_v2.json')
os.makedirs(FIGURE_DIRECTORY, exist_ok=True)


import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy import stats
from scipy.special import gammaln

from mcmc_diagnostics import (bulk_effective_sample_size, cross_check_against_arviz,
                              maximum_rhat, monte_carlo_standard_error,
                              tail_effective_sample_size)

plt.rcParams.update({'figure.figsize': (12, 6), 'font.size': 12, 'axes.grid': True,
                     'grid.alpha': 0.3})


def save_figure(filename):
    plt.savefig(os.path.join(FIGURE_DIRECTORY, filename), dpi=150, bbox_inches='tight')
    plt.close()

TELEMETRY_COLUMNS = ['Timestamp', 'CPU_Cores', 'CPU_Capacity_MHz', 'CPU_Usage_MHz', 'CPU_Usage_Pct',
                     'Mem_Provisioned_KB', 'Mem_Usage_KB', 'Disk_Read_KBps', 'Disk_Write_KBps',
                     'Net_Recv_KBps', 'Net_Trans_KBps']
VIRTUAL_MACHINE_COUNT = 50
MAXIMUM_OBSERVATIONS = 5000
TRAIN_FRACTION = 0.60
VALIDATION_FRACTION = 0.20
SAMPLING_INTERVAL_MINUTES = 5
FORECAST_HORIZON_STEPS = 1
FEATURE_WINDOW_STEPS = 6
COEFFICIENT_PRIOR_VARIANCE = 10.0
VARIANCE_PRIOR_SHAPE = 2.0
VARIANCE_PRIOR_RATE = 1.0
POSTERIOR_DRAWS = 10000
BURN_IN_DRAWS = 2000
CHAIN_COUNT = 4
REPEAT_COUNT = 3
LONG_RUN_DRAWS = 40000
PREDICTIVE_DRAW_COUNT = 2000
STUDENT_T_GRID = [2.0, 3.0, 4.0, 6.0, 10.0, 20.0]
RHAT_THRESHOLD = 1.01
BULK_ESS_THRESHOLD = 400.0
BOOTSTRAP_BLOCK_LENGTH = 50
BOOTSTRAP_REPLICATES = 2000
GAUSSIAN_SAMPLERS = ['MH', 'Adaptive MH (naive)', 'Preconditioned MH', 'Gibbs', 'HMC']
METHOD_COLORS = {'MH': 'steelblue', 'Adaptive MH (naive)': 'firebrick',
                 'Preconditioned MH': 'darkorange', 'Gibbs': 'coral', 'HMC': 'seagreen'}
REPORTED_PARAMETERS = [('Intercept', 0), ('beta_1', 1), ('beta_3', 3)]


# --------------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------------

def load_and_prepare_data():
    """Build a one-step-ahead forecasting design with a leakage-safe chronological split.

    Every predictor is lagged so that it is observable at the time the forecast is issued: the
    target is CPU usage at time t and all features are functions of information up to t-1. The
    horizon is therefore one sampling interval, five minutes.
    """
    csv_paths = sorted(glob.glob(os.path.join(DATA_DIRECTORY, '*.csv')))
    csv_paths = csv_paths[:VIRTUAL_MACHINE_COUNT]
    per_machine = []
    for csv_path in csv_paths:
        frame = pd.read_csv(csv_path, sep=';\t', header=0, engine='python')
        frame.columns = TELEMETRY_COLUMNS
        frame['VM_ID'] = os.path.basename(csv_path).replace('.csv', '')
        per_machine.append(frame)
    combined = pd.concat(per_machine, ignore_index=True)
    combined['Datetime'] = pd.to_datetime(combined['Timestamp'], unit='s')
    exogenous_columns = ['Mem_Usage_KB', 'Disk_Read_KBps', 'Disk_Write_KBps', 'Net_Recv_KBps',
                         'Net_Trans_KBps']
    engineered = []
    for machine_identifier in combined['VM_ID'].unique():
        frame = combined[combined['VM_ID'] == machine_identifier].sort_values('Datetime').copy()
        if len(frame) < 50:
            continue
        for column in exogenous_columns:
            frame['%s_lag1' % column] = frame[column].shift(FORECAST_HORIZON_STEPS)
        for lag in [1, 2, 3]:
            frame['CPU_lag_%d' % lag] = frame['CPU_Usage_Pct'].shift(lag)
        frame['CPU_rolling_mean'] = frame['CPU_Usage_Pct'].shift(1).rolling(
            window=FEATURE_WINDOW_STEPS).mean()
        frame['CPU_rolling_std'] = frame['CPU_Usage_Pct'].shift(1).rolling(
            window=FEATURE_WINDOW_STEPS).std()
        frame['earliest_information_time'] = frame['Datetime'].shift(FEATURE_WINDOW_STEPS)
        engineered.append(frame.dropna().reset_index(drop=True))
    features_frame = pd.concat(engineered, ignore_index=True)
    features_frame = features_frame.sort_values('Datetime').reset_index(drop=True)
    features_frame = features_frame.iloc[:MAXIMUM_OBSERVATIONS].reset_index(drop=True)
    predictor_names = (['%s_lag1' % column for column in exogenous_columns]
                       + ['CPU_lag_%d' % lag for lag in [1, 2, 3]]
                       + ['CPU_rolling_mean', 'CPU_rolling_std'])
    observation_count = len(features_frame)
    train_end = int(TRAIN_FRACTION * observation_count)
    validation_end = int((TRAIN_FRACTION + VALIDATION_FRACTION) * observation_count)
    train_boundary_time = features_frame['Datetime'].iloc[train_end - 1]
    validation_boundary_time = features_frame['Datetime'].iloc[validation_end - 1]
    earliest = features_frame['earliest_information_time']
    split_labels = np.array(['train'] * observation_count, dtype=object)
    split_labels[train_end:validation_end] = 'validation'
    split_labels[validation_end:] = 'test'
    embargoed = np.zeros(observation_count, dtype=bool)
    embargoed[train_end:validation_end] = earliest.iloc[train_end:validation_end] \
        <= train_boundary_time
    embargoed[validation_end:] = earliest.iloc[validation_end:] <= validation_boundary_time
    raw_predictors = features_frame[predictor_names].values
    raw_target = features_frame['CPU_Usage_Pct'].values
    train_mask = (split_labels == 'train') & ~embargoed
    predictor_mean = raw_predictors[train_mask].mean(axis=0)
    predictor_std = raw_predictors[train_mask].std(axis=0)
    predictor_std[predictor_std == 0] = 1.0
    target_mean = float(raw_target[train_mask].mean())
    target_std = float(raw_target[train_mask].std()) or 1.0
    scaled_predictors = (raw_predictors - predictor_mean) / predictor_std
    scaled_target = (raw_target - target_mean) / target_std
    design_matrix = np.column_stack([np.ones(observation_count), scaled_predictors])
    splits = {}
    for split_name in ('train', 'validation', 'test'):
        mask = (split_labels == split_name) & ~embargoed
        splits[split_name] = {'design': design_matrix[mask], 'target': scaled_target[mask],
                              'machine': features_frame['VM_ID'].values[mask],
                              'time': features_frame['Datetime'].values[mask]}
    return {'splits': splits, 'predictor_names': predictor_names, 'target_std': target_std,
            'target_mean': target_mean, 'embargoed_count': int(embargoed.sum()),
            'total_available': int(len(combined)),
            'distinct_machines': int(features_frame['VM_ID'].nunique()),
            'horizon_minutes': FORECAST_HORIZON_STEPS * SAMPLING_INTERVAL_MINUTES}


# --------------------------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------------------------

def log_prior_coefficients(coefficients):
    return -0.5 * len(coefficients) * np.log(2 * np.pi * COEFFICIENT_PRIOR_VARIANCE) \
        - 0.5 * np.sum(coefficients ** 2) / COEFFICIENT_PRIOR_VARIANCE


def log_prior_noise_variance(noise_variance):
    if noise_variance <= 0:
        return -np.inf
    return VARIANCE_PRIOR_SHAPE * np.log(VARIANCE_PRIOR_RATE) - gammaln(VARIANCE_PRIOR_SHAPE) \
        - (VARIANCE_PRIOR_SHAPE + 1) * np.log(noise_variance) - VARIANCE_PRIOR_RATE / noise_variance


def log_posterior(coefficients, noise_variance, design, target):
    prior_term = log_prior_noise_variance(noise_variance)
    if np.isinf(prior_term):
        return -np.inf
    residuals = target - design @ coefficients
    log_likelihood = -0.5 * len(target) * np.log(2 * np.pi * noise_variance) \
        - 0.5 * np.sum(residuals ** 2) / noise_variance
    return log_likelihood + log_prior_coefficients(coefficients) + prior_term


def log_posterior_unconstrained(parameter_vector, design, target):
    """Log posterior in the unconstrained parameterisation psi = log(sigma^2).

    The change of variables sigma^2 = exp(psi) contributes a Jacobian |d sigma^2 / d psi| =
    exp(psi) = sigma^2, so log(sigma^2) is added. Every sampler that works on the unconstrained
    scale (MH, both adaptive variants, HMC, emcee) uses this function.
    """
    coefficient_count = design.shape[1]
    log_variance = parameter_vector[coefficient_count]
    noise_variance = np.exp(log_variance)
    return log_posterior(parameter_vector[:coefficient_count], noise_variance, design,
                         target) + log_variance


def gradient_log_posterior_unconstrained(parameter_vector, design, target):
    coefficient_count = design.shape[1]
    coefficients = parameter_vector[:coefficient_count]
    noise_variance = np.exp(parameter_vector[coefficient_count])
    residuals = target - design @ coefficients
    coefficient_gradient = (design.T @ residuals) / noise_variance \
        - coefficients / COEFFICIENT_PRIOR_VARIANCE
    log_variance_gradient = -0.5 * len(target) + 0.5 * np.sum(residuals ** 2) / noise_variance \
        - (VARIANCE_PRIOR_SHAPE + 1) + VARIANCE_PRIOR_RATE / noise_variance + 1
    return np.concatenate([coefficient_gradient, [log_variance_gradient]])


def dispersed_starting_point(dimension, random_seed):
    """Overdispersed relative to the posterior, as split R-hat assumes."""
    generator = np.random.default_rng(10_000 + random_seed)
    coefficients = generator.normal(0.0, 2.0, size=dimension - 1)
    log_variance = generator.normal(0.0, 1.5)
    return np.concatenate([coefficients, [log_variance]])


# --------------------------------------------------------------------------------------------
# Samplers
# --------------------------------------------------------------------------------------------

def metropolis_hastings(design, target, draws, burn_in, random_seed=0, initial_parameters=None,
                        coefficient_step=0.001, log_variance_step=0.05):
    generator = np.random.default_rng(random_seed)
    coefficient_count = design.shape[1]
    dimension = coefficient_count + 1
    parameters = dispersed_starting_point(dimension, random_seed) if initial_parameters is None \
        else initial_parameters.copy()
    scales = np.concatenate([np.full(coefficient_count, coefficient_step), [log_variance_step]])
    current = log_posterior_unconstrained(parameters, design, target)
    coefficient_draws = np.zeros((draws, coefficient_count))
    variance_draws = np.zeros(draws)
    trace = np.zeros(draws + burn_in)
    accepted = 0
    for index in range(draws + burn_in):
        proposal = parameters + generator.normal(0, scales)
        proposed = log_posterior_unconstrained(proposal, design, target)
        if np.log(generator.uniform()) < proposed - current:
            parameters, current = proposal, proposed
            if index >= burn_in:
                accepted += 1
        trace[index] = current
        if index >= burn_in:
            coefficient_draws[index - burn_in] = parameters[:coefficient_count]
            variance_draws[index - burn_in] = np.exp(parameters[coefficient_count])
    return {'coefficients': coefficient_draws, 'variances': variance_draws,
            'acceptance_rate': accepted / draws, 'log_posterior': trace,
            'gradient_evaluations': 0, 'density_evaluations': draws + burn_in}


def adaptive_metropolis_hastings(design, target, draws, burn_in, random_seed=0,
                                 initial_parameters=None, adaptation_interval=200):
    """Haario-style adaptive Metropolis: proposal covariance estimated from the chain history."""
    generator = np.random.default_rng(random_seed)
    coefficient_count = design.shape[1]
    dimension = coefficient_count + 1
    parameters = dispersed_starting_point(dimension, random_seed) if initial_parameters is None \
        else initial_parameters.copy()
    scaling = 2.38 ** 2 / dimension
    covariance = np.diag(np.concatenate([np.full(coefficient_count, 0.001 ** 2), [0.05 ** 2]]))
    cholesky = np.linalg.cholesky(covariance)
    current = log_posterior_unconstrained(parameters, design, target)
    coefficient_draws = np.zeros((draws, coefficient_count))
    variance_draws = np.zeros(draws)
    trace = np.zeros(draws + burn_in)
    history = np.zeros((burn_in, dimension))
    accepted = 0
    for index in range(draws + burn_in):
        proposal = parameters + cholesky @ generator.normal(size=dimension)
        proposed = log_posterior_unconstrained(proposal, design, target)
        if np.log(generator.uniform()) < proposed - current:
            parameters, current = proposal, proposed
            if index >= burn_in:
                accepted += 1
        trace[index] = current
        if index < burn_in:
            history[index] = parameters
            if (index + 1) % adaptation_interval == 0 and index > 2 * dimension:
                empirical = np.cov(history[:index + 1].T)
                try:
                    cholesky = np.linalg.cholesky(scaling * empirical
                                                  + 1e-10 * np.eye(dimension))
                except np.linalg.LinAlgError:
                    pass
        else:
            coefficient_draws[index - burn_in] = parameters[:coefficient_count]
            variance_draws[index - burn_in] = np.exp(parameters[coefficient_count])
    return {'coefficients': coefficient_draws, 'variances': variance_draws,
            'acceptance_rate': accepted / draws, 'log_posterior': trace,
            'gradient_evaluations': 0, 'density_evaluations': draws + burn_in}


def preconditioned_metropolis_hastings(design, target, draws, burn_in, random_seed=0,
                                       initial_parameters=None, adaptation_interval=100,
                                       target_acceptance=0.234):
    """Metropolis preconditioned by the observed Fisher information.

    The metric is the Gaussian approximation to the posterior covariance of beta,
        (X'X / sigmahat^2 + I / tau^2)^-1  =  sigmahat^2 (X'X + sigmahat^2 I / tau^2)^-1,
    evaluated at the least-squares residual variance. It follows from one least-squares fit and
    does not require conjugacy. A Robbins-Monro rule tunes a global scale toward the optimal
    acceptance rate, and the empirical covariance of the post-transient burn-in refines the
    metric. Both adaptations are frozen before the retained draws begin, so the retained chain is
    a homogeneous Markov chain.
    """
    generator = np.random.default_rng(random_seed)
    observation_count, coefficient_count = design.shape
    dimension = coefficient_count + 1
    least_squares = np.linalg.lstsq(design, target, rcond=None)[0]
    residual_variance = float(np.mean((target - design @ least_squares) ** 2))
    coefficient_covariance = residual_variance * np.linalg.inv(
        design.T @ design + residual_variance * np.eye(coefficient_count)
        / COEFFICIENT_PRIOR_VARIANCE)
    base_covariance = np.zeros((dimension, dimension))
    base_covariance[:coefficient_count, :coefficient_count] = coefficient_covariance
    base_covariance[coefficient_count, coefficient_count] = 2.0 / observation_count
    scaling = 2.38 ** 2 / dimension
    log_scale = 0.0
    base_cholesky = np.linalg.cholesky(base_covariance)
    cholesky = np.exp(log_scale) * np.sqrt(scaling) * base_cholesky
    parameters = dispersed_starting_point(dimension, random_seed) if initial_parameters is None \
        else initial_parameters.copy()
    current = log_posterior_unconstrained(parameters, design, target)
    coefficient_draws = np.zeros((draws, coefficient_count))
    variance_draws = np.zeros(draws)
    trace = np.zeros(draws + burn_in)
    history = np.zeros((burn_in, dimension))
    transient = burn_in // 2
    accepted = 0
    for index in range(draws + burn_in):
        proposal = parameters + cholesky @ generator.normal(size=dimension)
        proposed = log_posterior_unconstrained(proposal, design, target)
        indicator = 0.0
        if np.log(generator.uniform()) < proposed - current:
            parameters, current = proposal, proposed
            indicator = 1.0
            if index >= burn_in:
                accepted += 1
        trace[index] = current
        if index < burn_in:
            history[index] = parameters
            log_scale += min(0.5, 5.0 / (index + 1) ** 0.6) * (indicator - target_acceptance)
            if (index + 1) % adaptation_interval == 0 and index > transient + 10 * dimension:
                empirical = np.cov(history[transient:index + 1].T)
                try:
                    base_cholesky = np.linalg.cholesky(
                        empirical + 1e-10 * np.trace(empirical) / dimension * np.eye(dimension))
                except np.linalg.LinAlgError:
                    pass
            cholesky = np.exp(log_scale) * np.sqrt(scaling) * base_cholesky
        else:
            coefficient_draws[index - burn_in] = parameters[:coefficient_count]
            variance_draws[index - burn_in] = np.exp(parameters[coefficient_count])
    return {'coefficients': coefficient_draws, 'variances': variance_draws,
            'acceptance_rate': accepted / draws, 'log_posterior': trace,
            'gradient_evaluations': 0, 'density_evaluations': draws + burn_in}


def gibbs_sampler(design, target, draws, burn_in, random_seed=0, initial_parameters=None):
    """Full conditionals: beta is Gaussian, sigma^2 is Inverse-Gamma in shape-rate form.

    numpy draws Gamma with a scale argument, so the rate b_n is passed as 1/b_n and the result
    inverted to obtain the Inverse-Gamma draw.
    """
    generator = np.random.default_rng(random_seed)
    observation_count, coefficient_count = design.shape
    cross_product = design.T @ design
    cross_target = design.T @ target
    dimension = coefficient_count + 1
    start = dispersed_starting_point(dimension, random_seed) if initial_parameters is None \
        else initial_parameters
    coefficients = start[:coefficient_count].copy()
    noise_variance = float(np.exp(start[coefficient_count]))
    coefficient_draws = np.zeros((draws, coefficient_count))
    variance_draws = np.zeros(draws)
    trace = np.zeros(draws + burn_in)
    identity = np.eye(coefficient_count)
    for index in range(draws + burn_in):
        precision = cross_product / noise_variance + identity / COEFFICIENT_PRIOR_VARIANCE
        covariance = np.linalg.inv(precision)
        mean_vector = covariance @ (cross_target / noise_variance)
        coefficients = generator.multivariate_normal(mean_vector, covariance)
        residuals = target - design @ coefficients
        shape = VARIANCE_PRIOR_SHAPE + observation_count / 2.0
        rate = VARIANCE_PRIOR_RATE + 0.5 * np.sum(residuals ** 2)
        noise_variance = 1.0 / generator.gamma(shape, 1.0 / rate)
        trace[index] = log_posterior(coefficients, noise_variance, design, target)
        if index >= burn_in:
            coefficient_draws[index - burn_in] = coefficients
            variance_draws[index - burn_in] = noise_variance
    return {'coefficients': coefficient_draws, 'variances': variance_draws,
            'acceptance_rate': 1.0, 'log_posterior': trace, 'gradient_evaluations': 0,
            'density_evaluations': draws + burn_in}


def hamiltonian_monte_carlo(design, target, draws, burn_in, random_seed=0,
                            initial_parameters=None, step_size=0.002, leapfrog_steps=15):
    generator = np.random.default_rng(random_seed)
    coefficient_count = design.shape[1]
    dimension = coefficient_count + 1
    parameters = dispersed_starting_point(dimension, random_seed) if initial_parameters is None \
        else initial_parameters.copy()
    current = log_posterior_unconstrained(parameters, design, target)
    coefficient_draws = np.zeros((draws, coefficient_count))
    variance_draws = np.zeros(draws)
    trace = np.zeros(draws + burn_in)
    accepted = 0
    gradient_evaluations = 0
    for index in range(draws + burn_in):
        momentum = generator.normal(size=dimension)
        proposal = parameters.copy()
        proposed_momentum = momentum.copy()
        gradient = gradient_log_posterior_unconstrained(proposal, design, target)
        gradient_evaluations += 1
        proposed_momentum += 0.5 * step_size * gradient
        for leapfrog_index in range(leapfrog_steps):
            proposal += step_size * proposed_momentum
            gradient = gradient_log_posterior_unconstrained(proposal, design, target)
            gradient_evaluations += 1
            if leapfrog_index < leapfrog_steps - 1:
                proposed_momentum += step_size * gradient
        proposed_momentum += 0.5 * step_size * gradient
        proposed = log_posterior_unconstrained(proposal, design, target)
        energy_change = (proposed - 0.5 * np.sum(proposed_momentum ** 2)) \
            - (current - 0.5 * np.sum(momentum ** 2))
        if np.log(generator.uniform()) < energy_change:
            parameters, current = proposal, proposed
            if index >= burn_in:
                accepted += 1
        trace[index] = current
        if index >= burn_in:
            coefficient_draws[index - burn_in] = parameters[:coefficient_count]
            variance_draws[index - burn_in] = np.exp(parameters[coefficient_count])
    return {'coefficients': coefficient_draws, 'variances': variance_draws,
            'acceptance_rate': accepted / draws, 'log_posterior': trace,
            'gradient_evaluations': gradient_evaluations,
            'density_evaluations': draws + burn_in}


def robust_student_t_gibbs(design, target, draws, burn_in, degrees_of_freedom, random_seed=0):
    """Student-t regression through the normal scale-mixture representation.

    y_i | beta, sigma^2, w_i ~ N(x_i' beta, sigma^2 / w_i) with w_i ~ Gamma(nu/2, nu/2) in
    shape-rate form marginalises to a Student-t with nu degrees of freedom and scale sigma.
    """
    generator = np.random.default_rng(random_seed)
    observation_count, coefficient_count = design.shape
    coefficients = np.zeros(coefficient_count)
    scale_squared = 1.0
    weights = np.ones(observation_count)
    coefficient_draws = np.zeros((draws, coefficient_count))
    scale_draws = np.zeros(draws)
    identity = np.eye(coefficient_count)
    for index in range(draws + burn_in):
        weighted_design = design * weights[:, None]
        precision = (design.T @ weighted_design) / scale_squared \
            + identity / COEFFICIENT_PRIOR_VARIANCE
        covariance = np.linalg.inv(precision)
        coefficients = generator.multivariate_normal(
            covariance @ (weighted_design.T @ target / scale_squared), covariance)
        residuals = target - design @ coefficients
        scale_squared = 1.0 / generator.gamma(
            VARIANCE_PRIOR_SHAPE + observation_count / 2.0,
            1.0 / (VARIANCE_PRIOR_RATE + 0.5 * np.sum(weights * residuals ** 2)))
        weights = generator.gamma((degrees_of_freedom + 1.0) / 2.0,
                                  2.0 / (degrees_of_freedom + residuals ** 2 / scale_squared))
        if index >= burn_in:
            coefficient_draws[index - burn_in] = coefficients
            scale_draws[index - burn_in] = scale_squared
    return {'coefficients': coefficient_draws, 'variances': scale_draws, 'acceptance_rate': 1.0}


# --------------------------------------------------------------------------------------------
# References
# --------------------------------------------------------------------------------------------

def analytical_posterior_reference(design, target, grid_size=6000):
    """Deterministic reference for the posterior mean, by one-dimensional quadrature.

    The prior on beta does not scale with sigma^2, so the joint posterior is not a standard
    Normal-Inverse-Gamma. Marginalising beta analytically is still possible because
        y | sigma^2 ~ N(0, sigma^2 I + tau^2 X X'),
    whose log density is evaluated with Sylvester's determinant identity and the Woodbury
    identity in O(n p^2). The remaining one-dimensional density over sigma^2 is integrated on a
    grid, giving a reference that involves no sampling of any kind. It validates the likelihood
    and prior algebra, not merely our sampling of them.
    """
    observation_count, coefficient_count = design.shape
    cross_product = design.T @ design
    cross_target = design.T @ target
    total_sum_of_squares = float(target @ target)
    identity = np.eye(coefficient_count)
    log_grid = np.linspace(np.log(1e-4), np.log(1e2), grid_size)
    variance_grid = np.exp(log_grid)
    log_density = np.empty(grid_size)
    conditional_means = np.empty((grid_size, coefficient_count))
    for index, noise_variance in enumerate(variance_grid):
        precision = cross_product / noise_variance + identity / COEFFICIENT_PRIOR_VARIANCE
        cholesky = np.linalg.cholesky(precision)
        solved = np.linalg.solve(precision, cross_target / noise_variance)
        conditional_means[index] = solved
        log_determinant = observation_count * np.log(noise_variance) \
            + 2.0 * np.sum(np.log(np.diag(cholesky))) \
            + coefficient_count * np.log(COEFFICIENT_PRIOR_VARIANCE)
        quadratic = total_sum_of_squares / noise_variance \
            - (cross_target / noise_variance) @ solved
        log_density[index] = -0.5 * (observation_count * np.log(2 * np.pi) + log_determinant
                                     + quadratic) + log_prior_noise_variance(noise_variance) \
            + np.log(noise_variance)
    log_density -= log_density.max()
    density = np.exp(log_density)
    normaliser = np.trapezoid(density, log_grid)
    weights = density / normaliser
    posterior_mean_variance = float(np.trapezoid(weights * variance_grid, log_grid))
    posterior_mean_coefficients = np.array(
        [np.trapezoid(weights * conditional_means[:, j], log_grid)
         for j in range(coefficient_count)])
    return {'posterior_mean_coefficients': posterior_mean_coefficients.tolist(),
            'posterior_mean_variance': posterior_mean_variance,
            'grid_size': grid_size, 'method': 'one-dimensional quadrature over log sigma^2'}


def run_emcee_reference(design, target, walkers=40, steps=12000, discard=4000,
                        random_seed=42):
    import emcee
    generator = np.random.default_rng(random_seed)
    coefficient_count = design.shape[1]
    dimension = coefficient_count + 1
    least_squares = np.linalg.lstsq(design, target, rcond=None)[0]
    residual_variance = float(np.mean((target - design @ least_squares) ** 2))
    centre = np.concatenate([least_squares, [np.log(residual_variance)]])
    start = centre + 1e-3 * generator.normal(size=(walkers, dimension))
    sampler = emcee.EnsembleSampler(walkers, dimension, log_posterior_unconstrained,
                                    args=(design, target))
    started = time.time()
    sampler.run_mcmc(start, steps, progress=False)
    elapsed = time.time() - started
    retained = sampler.get_chain(discard=discard, flat=False)
    flat = retained.reshape(-1, dimension)
    walker_chains = [retained[:, walker, :] for walker in range(walkers)]
    diagnostics = {}
    for label, index in REPORTED_PARAMETERS:
        chains = [chain[:, index] for chain in walker_chains]
        diagnostics[label] = {'rhat': maximum_rhat(chains),
                              'bulk_ess': bulk_effective_sample_size(chains),
                              'tail_ess': tail_effective_sample_size(chains)}
    variance_chains = [np.exp(chain[:, coefficient_count]) for chain in walker_chains]
    diagnostics['sigma2'] = {'rhat': maximum_rhat(variance_chains),
                             'bulk_ess': bulk_effective_sample_size(variance_chains),
                             'tail_ess': tail_effective_sample_size(variance_chains)}
    return {'posterior_mean_coefficients': flat[:, :coefficient_count].mean(axis=0).tolist(),
            'posterior_mean_variance': float(np.exp(flat[:, coefficient_count]).mean()),
            'time': elapsed, 'walkers': walkers, 'steps': steps, 'discarded_steps': discard,
            'total_draws': int(flat.shape[0]),
            'acceptance_rate': float(np.mean(sampler.acceptance_fraction)),
            'diagnostics': diagnostics,
            'worst_rhat': max(entry['rhat'] for entry in diagnostics.values()),
            'minimum_bulk_ess': min(entry['bulk_ess'] for entry in diagnostics.values()),
            'library': 'emcee %s' % emcee.__version__}


# --------------------------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------------------------

def summarise_sampler(coefficient_chains, variance_chains, elapsed_time, acceptance_rate,
                      gradient_evaluations, draws_per_chain):
    """Aggregate diagnostics.

    Every diagnostic is computed jointly across all chains for one parameter at a time. The
    reported scalar is then the WORST value over the monitored parameters: the maximum R-hat and
    the minimum bulk and tail ESS. Taking the worst rather than an average means a single badly
    behaved coordinate cannot be hidden by well behaved ones.
    """
    per_parameter = {}
    for label, index in REPORTED_PARAMETERS:
        chains = [chain[:, index] for chain in coefficient_chains]
        per_parameter[label] = {'rhat': maximum_rhat(chains),
                                'bulk_ess': bulk_effective_sample_size(chains),
                                'tail_ess': tail_effective_sample_size(chains),
                                'mcse': monte_carlo_standard_error(chains)}
    per_parameter['sigma2'] = {'rhat': maximum_rhat(variance_chains),
                               'bulk_ess': bulk_effective_sample_size(variance_chains),
                               'tail_ess': tail_effective_sample_size(variance_chains),
                               'mcse': monte_carlo_standard_error(variance_chains)}
    worst_rhat = max(entry['rhat'] for entry in per_parameter.values())
    minimum_bulk = min(entry['bulk_ess'] for entry in per_parameter.values())
    minimum_tail = min(entry['tail_ess'] for entry in per_parameter.values())
    total_draws = draws_per_chain * len(coefficient_chains)
    return {'acceptance_rate': acceptance_rate, 'time': elapsed_time,
            'total_draws': total_draws, 'worst_rhat': worst_rhat,
            'min_bulk_ess': minimum_bulk, 'min_tail_ess': minimum_tail,
            'bulk_ess_per_second': minimum_bulk / elapsed_time,
            'bulk_ess_per_gradient': (minimum_bulk / gradient_evaluations)
            if gradient_evaluations else None,
            'gradient_evaluations': gradient_evaluations,
            'mcse_intercept': per_parameter['Intercept']['mcse'],
            'per_parameter': per_parameter,
            'converged': bool(worst_rhat < RHAT_THRESHOLD and minimum_bulk >= BULK_ESS_THRESHOLD),
            'posterior_mean_coefficients': np.vstack(coefficient_chains).mean(axis=0).tolist(),
            'posterior_mean_variance': float(np.concatenate(variance_chains).mean())}


def posterior_predictive_evaluation(coefficient_draws, variance_draws, design, target,
                                    target_std, random_seed=0, degrees_of_freedom=None):
    """Posterior predictive intervals.

    For each retained draw (beta_s, sigma^2_s) a replicate observation is generated as
        y* = x' beta_s + sigma_s * e,      e ~ N(0,1) or t_nu,
    so the interval carries both parameter uncertainty, through the spread of beta_s, and
    observation noise, through sigma_s. Intervals are empirical quantiles of these replicates,
    not mean plus a multiple of a standard deviation.
    """
    generator = np.random.default_rng(random_seed)
    selection = generator.choice(len(coefficient_draws),
                                 size=min(PREDICTIVE_DRAW_COUNT, len(coefficient_draws)),
                                 replace=False)
    coefficients = coefficient_draws[selection]
    scales = np.sqrt(variance_draws[selection])[:, None]
    conditional_means = coefficients @ design.T
    if degrees_of_freedom is None:
        noise = generator.normal(size=conditional_means.shape)
    else:
        noise = generator.standard_t(degrees_of_freedom, size=conditional_means.shape)
    replicates = conditional_means + scales * noise
    log_density = predictive_log_density(conditional_means, scales, target, degrees_of_freedom)
    point_predictions = conditional_means.mean(axis=0)
    errors = target - point_predictions
    results = {'rmse': float(np.sqrt(np.mean(errors ** 2))),
               'median_absolute_error': float(np.median(np.abs(errors))),
               'rmse_percentage_points': float(np.sqrt(np.mean(errors ** 2)) * target_std),
               'median_absolute_error_percentage_points': float(
                   np.median(np.abs(errors)) * target_std),
               'point_predictions': point_predictions}
    for level in (0.50, 0.95):
        lower = np.quantile(replicates, (1 - level) / 2, axis=0)
        upper = np.quantile(replicates, 1 - (1 - level) / 2, axis=0)
        inside = (target >= lower) & (target <= upper)
        key = int(level * 100)
        results['coverage_%d' % key] = float(inside.mean())
        results['width_%d' % key] = float(np.mean(upper - lower))
        results['width_%d_percentage_points' % key] = float(np.mean(upper - lower) * target_std)
        results['coverage_%d_interval' % key] = block_bootstrap_interval(inside, random_seed)
    results['mean_log_predictive_density'] = log_density
    return results


def predictive_log_density(conditional_means, scales, target, degrees_of_freedom=None):
    """Mean log pointwise predictive density, evaluated exactly for each posterior draw.

    The predictive density is the posterior mixture p(y*|data) = (1/S) sum_s p(y*|theta_s), and
    each component is a Gaussian or Student-t with known mean and scale, so it is evaluated in
    closed form rather than estimated from simulated replicates. This avoids the bias a kernel
    density estimate would introduce in the tails, which is exactly where the two likelihoods
    differ and therefore where the model choice is decided.
    """
    from scipy.special import logsumexp
    if degrees_of_freedom is None:
        component_log_density = stats.norm.logpdf(target[None, :], loc=conditional_means,
                                                  scale=scales)
    else:
        component_log_density = stats.t.logpdf(target[None, :], degrees_of_freedom,
                                               loc=conditional_means, scale=scales)
    draw_count = conditional_means.shape[0]
    pointwise = logsumexp(component_log_density, axis=0) - np.log(draw_count)
    return float(np.mean(pointwise))


def block_bootstrap_interval(indicator, random_seed=0):
    """Moving-block bootstrap confidence interval for a coverage rate under serial dependence."""
    generator = np.random.default_rng(random_seed + 991)
    length = len(indicator)
    if length < BOOTSTRAP_BLOCK_LENGTH * 2:
        return [float('nan'), float('nan')]
    block_count = int(np.ceil(length / BOOTSTRAP_BLOCK_LENGTH))
    maximum_start = length - BOOTSTRAP_BLOCK_LENGTH
    starts = generator.integers(0, maximum_start + 1, size=(BOOTSTRAP_REPLICATES, block_count))
    offsets = np.arange(BOOTSTRAP_BLOCK_LENGTH)
    estimates = np.empty(BOOTSTRAP_REPLICATES)
    for replicate in range(BOOTSTRAP_REPLICATES):
        indices = (starts[replicate][:, None] + offsets[None, :]).reshape(-1)[:length]
        estimates[replicate] = indicator[indices].mean()
    return [float(np.quantile(estimates, 0.025)), float(np.quantile(estimates, 0.975))]


def residual_diagnostics(design, target, machine_labels, target_std, maximum_lag=40):
    least_squares = np.linalg.lstsq(design, target, rcond=None)[0]
    residuals = target - design @ least_squares
    centered = residuals - residuals.mean()
    autocorrelation = np.correlate(centered, centered, mode='full')[len(centered) - 1:]
    autocorrelation = autocorrelation / autocorrelation[0]
    length = len(residuals)
    lags = np.arange(1, maximum_lag + 1)
    statistic = length * (length + 2) * np.sum(
        autocorrelation[1:maximum_lag + 1] ** 2 / (length - lags))
    per_machine_std = []
    for machine in np.unique(machine_labels):
        mask = machine_labels == machine
        if mask.sum() >= 10:
            per_machine_std.append(float(residuals[mask].std()))
    if not per_machine_std:
        per_machine_std = [float(residuals.std())]
    return {'autocorrelation': autocorrelation[:maximum_lag + 1].tolist(),
            'ljung_box_statistic': float(statistic), 'ljung_box_lags': int(maximum_lag),
            'ljung_box_p_value': float(stats.chi2.sf(statistic, maximum_lag)),
            'lag1_autocorrelation': float(autocorrelation[1]),
            'excess_kurtosis': float(stats.kurtosis(residuals)),
            'skewness': float(stats.skew(residuals)),
            'residual_sd': float(residuals.std()),
            'residual_mad': float(np.median(np.abs(residuals))),
            'sd_to_robust_sd_ratio': float(residuals.std()
                                           / (1.4826 * np.median(np.abs(residuals)))),
            'per_machine_residual_sd_min': float(np.min(per_machine_std)),
            'per_machine_residual_sd_max': float(np.max(per_machine_std)),
            'per_machine_residual_sd_median': float(np.median(per_machine_std)),
            'per_machine_count': len(per_machine_std),
            'pooled_residual_sd_percentage_points': float(residuals.std() * target_std)}


def posterior_geometry(design, noise_variance):
    precision = design.T @ design / noise_variance \
        + np.eye(design.shape[1]) / COEFFICIENT_PRIOR_VARIANCE
    covariance = np.linalg.inv(precision)
    eigenvalues = np.linalg.eigvalsh(covariance)
    standard_deviations = np.sqrt(np.diag(covariance))
    correlation = covariance / np.outer(standard_deviations, standard_deviations)
    off_diagonal = ~np.eye(len(correlation), dtype=bool)
    return {'condition_number': float(eigenvalues.max() / eigenvalues.min()),
            'max_absolute_correlation': float(np.abs(correlation[off_diagonal]).max()),
            'narrowest_direction_sd': float(np.sqrt(eigenvalues.min())),
            'widest_direction_sd': float(np.sqrt(eigenvalues.max())),
            'correlation_matrix': correlation}


# --------------------------------------------------------------------------------------------
# Studies
# --------------------------------------------------------------------------------------------

def run_sampler_repeatedly(sampler_function, design, target, label, extra_arguments=None):
    """Run the full multi-chain experiment several times to quantify run-to-run variability."""
    extra_arguments = extra_arguments or {}
    summaries, pooled_coefficients, pooled_variances = [], None, None
    for repeat_index in range(REPEAT_COUNT):
        coefficient_chains, variance_chains, acceptance_rates = [], [], []
        gradient_total = 0
        started = time.time()
        for chain_index in range(CHAIN_COUNT):
            output = sampler_function(design, target, POSTERIOR_DRAWS, BURN_IN_DRAWS,
                                      random_seed=1000 * repeat_index + chain_index,
                                      **extra_arguments)
            coefficient_chains.append(output['coefficients'])
            variance_chains.append(output['variances'])
            acceptance_rates.append(output['acceptance_rate'])
            gradient_total += output.get('gradient_evaluations', 0)
        elapsed = time.time() - started
        summary = summarise_sampler(coefficient_chains, variance_chains, elapsed,
                                    float(np.mean(acceptance_rates)), gradient_total,
                                    POSTERIOR_DRAWS)
        summaries.append(summary)
        if repeat_index == 0:
            pooled_coefficients, pooled_variances = coefficient_chains, variance_chains
    primary = dict(summaries[0])
    for key in ('time', 'min_bulk_ess', 'min_tail_ess', 'bulk_ess_per_second', 'worst_rhat'):
        values = np.array([summary[key] for summary in summaries], dtype=float)
        primary['%s_mean' % key] = float(values.mean())
        primary['%s_sd' % key] = float(values.std(ddof=1)) if len(values) > 1 else 0.0
    primary['repeats'] = REPEAT_COUNT
    primary['label'] = label
    return primary, pooled_coefficients, pooled_variances


def hamiltonian_sensitivity_study(design, target, warm_start):
    """Vary step size and trajectory length jointly, reporting ESS per gradient evaluation.

    The grid runs are warm started at the least-squares fit and given a long burn-in. A short
    run from a dispersed start would not have converged for every configuration, and an ESS
    computed from an unconverged chain measures nothing, so each cell also reports its R-hat and
    is marked converged or not.
    """
    results = []
    for step_size in (0.001, 0.002, 0.004, 0.008):
        for leapfrog_steps in (5, 10, 20):
            chains, variance_chains, acceptances = [], [], []
            gradient_total = 0
            started = time.time()
            for chain_index in range(4):
                perturbed = warm_start + 0.01 * np.random.default_rng(
                    900 + chain_index).normal(size=len(warm_start))
                output = hamiltonian_monte_carlo(design, target, 4000, 1500,
                                                 random_seed=77 + chain_index,
                                                 initial_parameters=perturbed,
                                                 step_size=step_size,
                                                 leapfrog_steps=leapfrog_steps)
                chains.append(output['coefficients'])
                variance_chains.append(output['variances'])
                acceptances.append(output['acceptance_rate'])
                gradient_total += output['gradient_evaluations']
            elapsed = time.time() - started
            bulk = min(bulk_effective_sample_size([chain[:, index] for chain in chains])
                       for _, index in REPORTED_PARAMETERS)
            worst = max(max(maximum_rhat([chain[:, index] for chain in chains])
                            for _, index in REPORTED_PARAMETERS),
                        maximum_rhat(variance_chains))
            converged = bool(worst < RHAT_THRESHOLD)
            results.append({'step_size': step_size, 'leapfrog_steps': leapfrog_steps,
                            'acceptance': float(np.mean(acceptances)), 'bulk_ess': float(bulk),
                            'worst_rhat': float(worst), 'converged': converged,
                            'gradient_evaluations': int(gradient_total),
                            'bulk_ess_per_gradient': float(bulk / gradient_total),
                            'bulk_ess_per_second': float(bulk / elapsed), 'time': elapsed})
            print('    eps=%.3f L=%2d -> acc %.3f | R-hat %.4f | bulk ESS %7.1f | ESS/grad %.2e%s'
                  % (step_size, leapfrog_steps, results[-1]['acceptance'], worst, bulk,
                     results[-1]['bulk_ess_per_gradient'], '' if converged else '  [NOT conv]'))
    return results


def metropolis_sensitivity_study(design, target):
    results = []
    for step in (0.0005, 0.001, 0.005, 0.01, 0.02, 0.05):
        output = metropolis_hastings(design, target, 3000, 500, random_seed=5,
                                     coefficient_step=step)
        results.append({'step': step, 'acceptance': float(output['acceptance_rate'])})
    return results


def initialisation_study(design, target, least_squares_coefficients):
    """Iterations needed to reach the stationary level of the log posterior, from four starts.

    The stationary level is the median log posterior over the final 500 iterations of a
    1,500-iteration run. A chain is deemed to have reached it at the first iteration after which
    the log posterior stays within 1 percent of that level for 50 consecutive iterations.
    Requiring persistence rather than a single crossing stops a chain being credited for merely
    passing through the region on its way elsewhere.
    """
    coefficient_count = design.shape[1]
    starts = {
        'zeros': np.zeros(coefficient_count + 1),
        'least squares fit': np.concatenate([least_squares_coefficients, [np.log(0.15)]]),
        'dispersed (all +3)': np.concatenate([np.full(coefficient_count, 3.0), [np.log(5.0)]]),
        'extreme variance': np.concatenate([np.zeros(coefficient_count), [np.log(100.0)]]),
    }
    samplers = {'Preconditioned MH': preconditioned_metropolis_hastings, 'Gibbs': gibbs_sampler,
                'HMC': hamiltonian_monte_carlo}
    persistence = 50
    results = {}
    for sampler_name, sampler_function in samplers.items():
        results[sampler_name] = {}
        for start_name, start_vector in starts.items():
            output = sampler_function(design, target, 1000, 500, random_seed=7,
                                      initial_parameters=start_vector)
            trace = output['log_posterior']
            level = float(np.median(trace[-500:]))
            within = np.abs(trace - level) < 0.01 * abs(level)
            reached, run_length = -1, 0
            for position, flag in enumerate(within):
                run_length = run_length + 1 if flag else 0
                if run_length >= persistence:
                    reached = position - persistence + 1
                    break
            results[sampler_name][start_name] = {'iterations_to_stationarity': int(reached),
                                                 'log_posterior_trace': trace[:400].tolist(),
                                                 'stationary_level': level}
    return results


def select_likelihood_on_validation(train, validation, target_std):
    """Choose the likelihood and the degrees of freedom using validation data only."""
    candidates = []
    gaussian = gibbs_sampler(train['design'], train['target'], POSTERIOR_DRAWS, BURN_IN_DRAWS,
                             random_seed=3)
    gaussian_metrics = posterior_predictive_evaluation(
        gaussian['coefficients'], gaussian['variances'], validation['design'],
        validation['target'], target_std, random_seed=11)
    candidates.append({'likelihood': 'Gaussian', 'degrees_of_freedom': None,
                       'validation_log_density': gaussian_metrics['mean_log_predictive_density'],
                       'validation_coverage_50': gaussian_metrics['coverage_50'],
                       'validation_coverage_95': gaussian_metrics['coverage_95'],
                       'validation_median_absolute_error':
                           gaussian_metrics['median_absolute_error']})
    print('    Gaussian            -> validation log density %.4f, 50%% coverage %.3f'
          % (gaussian_metrics['mean_log_predictive_density'], gaussian_metrics['coverage_50']))
    for degrees_of_freedom in STUDENT_T_GRID:
        fitted = robust_student_t_gibbs(train['design'], train['target'], POSTERIOR_DRAWS,
                                        BURN_IN_DRAWS, degrees_of_freedom, random_seed=3)
        metrics = posterior_predictive_evaluation(
            fitted['coefficients'], fitted['variances'], validation['design'],
            validation['target'], target_std, random_seed=11,
            degrees_of_freedom=degrees_of_freedom)
        candidates.append({'likelihood': 'Student-t', 'degrees_of_freedom': degrees_of_freedom,
                           'validation_log_density': metrics['mean_log_predictive_density'],
                           'validation_coverage_50': metrics['coverage_50'],
                           'validation_coverage_95': metrics['coverage_95'],
                           'validation_median_absolute_error':
                               metrics['median_absolute_error']})
        print('    Student-t nu=%-5.1f -> validation log density %.4f, 50%% coverage %.3f'
              % (degrees_of_freedom, metrics['mean_log_predictive_density'],
                 metrics['coverage_50']))
    best = max(candidates, key=lambda entry: entry['validation_log_density'])
    return candidates, best


# --------------------------------------------------------------------------------------------
# Figures
# --------------------------------------------------------------------------------------------

def generate_figures(chain_storage, summaries, residuals_test, geometry, initialisation,
                     likelihood_candidates, hmc_grid, predictions, dataset, raw_frame_sample):
    method_names = [name for name in GAUSSIAN_SAMPLERS if name in summaries]
    converged_names = [name for name in method_names if summaries[name]['converged']]

    figure, axes = plt.subplots(2, 3, figsize=(17, 9))
    figure.suptitle('Telemetry distributions on a logarithmic count scale', fontsize=15,
                    fontweight='bold')
    columns = [('CPU_Usage_Pct', 'CPU usage (%)', False),
               ('Mem_Usage_KB', 'Memory usage (KB)', True),
               ('Disk_Read_KBps', 'Disk read (KB/s)', True),
               ('Disk_Write_KBps', 'Disk write (KB/s)', True),
               ('Net_Recv_KBps', 'Network received (KB/s)', True),
               ('Net_Trans_KBps', 'Network transmitted (KB/s)', True)]
    for axis, (column, label, use_log_x) in zip(axes.flat, columns):
        values = raw_frame_sample[column].dropna().values
        values = values[np.isfinite(values)]
        if use_log_x:
            positive = values[values > 0]
            if len(positive):
                bins = np.logspace(np.log10(max(positive.min(), 1e-3)),
                                   np.log10(positive.max()), 60)
                axis.hist(positive, bins=bins, color='steelblue', edgecolor='black',
                          linewidth=0.3)
                axis.set_xscale('log')
        else:
            axis.hist(values, bins=60, color='steelblue', edgecolor='black', linewidth=0.3)
        axis.set_yscale('log')
        axis.set_title(label)
        axis.set_ylabel('Count (log scale)')
    plt.tight_layout()
    save_figure('fig_eda.png')

    figure, axes = plt.subplots(1, 3, figsize=(18, 5))
    figure.suptitle('Posterior geometry and the effect of preconditioning', fontsize=15,
                    fontweight='bold')
    image = axes[0].imshow(geometry['correlation_matrix'], cmap='RdBu_r', vmin=-1, vmax=1)
    labels = ['int'] + [name[:9] for name in dataset['predictor_names']]
    axes[0].set_xticks(range(len(labels)))
    axes[0].set_yticks(range(len(labels)))
    axes[0].set_xticklabels(labels, rotation=90, fontsize=7)
    axes[0].set_yticklabels(labels, fontsize=7)
    axes[0].set_title('Posterior correlation of coefficients')
    figure.colorbar(image, ax=axes[0], shrink=0.8)
    pair = [name for name in ['MH', 'Preconditioned MH'] if name in summaries]
    axes[1].bar(pair, [summaries[name]['min_bulk_ess'] for name in pair],
                yerr=[summaries[name]['min_bulk_ess_sd'] for name in pair],
                color=[METHOD_COLORS[name] for name in pair], edgecolor='black', capsize=6)
    axes[1].set_yscale('log')
    axes[1].set_title('Bulk ESS before and after preconditioning')
    axes[1].set_ylabel('Bulk ESS (log scale), error bars over %d repeats' % REPEAT_COUNT)
    for position, name in enumerate(pair):
        axes[1].text(position, summaries[name]['min_bulk_ess'],
                     '%.0f' % summaries[name]['min_bulk_ess'], ha='center', va='bottom')
    for name in pair:
        chain = chain_storage[name]['coefficients'][0][:, 0]
        centered = chain - chain.mean()
        autocorrelation = np.correlate(centered, centered, mode='full')[len(centered) - 1:]
        autocorrelation = autocorrelation / autocorrelation[0]
        axes[2].plot(autocorrelation[:300], label=name, color=METHOD_COLORS[name], linewidth=2)
    axes[2].axhline(0, color='black', linewidth=1)
    axes[2].set_title('Autocorrelation of the intercept')
    axes[2].set_xlabel('Lag')
    axes[2].set_ylabel('ACF')
    axes[2].legend()
    plt.tight_layout()
    save_figure('fig_preconditioning.png')

    figure, axes = plt.subplots(1, 2, figsize=(16, 5.5))
    figure.suptitle('Convergence: rank-normalised R-hat and bulk ESS', fontsize=15,
                    fontweight='bold')
    positions = np.arange(len(method_names))
    axes[0].bar(positions, [summaries[name]['worst_rhat'] for name in method_names],
                color=[METHOD_COLORS[name] for name in method_names], edgecolor='black')
    axes[0].axhline(RHAT_THRESHOLD, color='darkred', linestyle=':', linewidth=1.5,
                    label='threshold 1.01')
    axes[0].set_yscale('log')
    axes[0].set_xticks(positions)
    axes[0].set_xticklabels(method_names, rotation=18, ha='right')
    axes[0].set_ylabel('Worst R-hat over monitored parameters')
    axes[0].set_title('Convergence diagnostic')
    axes[0].legend()
    axes[1].bar(positions, [summaries[name]['min_bulk_ess'] for name in method_names],
                yerr=[summaries[name]['min_bulk_ess_sd'] for name in method_names],
                color=[METHOD_COLORS[name] for name in method_names], edgecolor='black',
                capsize=5)
    axes[1].axhline(BULK_ESS_THRESHOLD, color='darkred', linestyle=':', linewidth=1.5,
                    label='threshold 400')
    axes[1].set_yscale('log')
    axes[1].set_xticks(positions)
    axes[1].set_xticklabels(method_names, rotation=18, ha='right')
    axes[1].set_ylabel('Minimum bulk ESS')
    axes[1].set_title('Effective sample size, error bars over %d repeats' % REPEAT_COUNT)
    axes[1].legend()
    plt.tight_layout()
    save_figure('fig_convergence.png')

    figure, axes = plt.subplots(2, 2, figsize=(15, 9))
    figure.suptitle('Posterior distributions by sampler', fontsize=15, fontweight='bold')
    panels = [('Intercept', 0), ('beta_1', 1), ('beta_3', 3), ('sigma2', None)]
    for axis, (label, index) in zip(axes.flat, panels):
        for name in method_names:
            if index is None:
                values = np.concatenate(chain_storage[name]['variances'])
            else:
                values = np.vstack(chain_storage[name]['coefficients'])[:, index]
            axis.hist(values, bins=80, density=True, alpha=0.45, label=name,
                      color=METHOD_COLORS[name])
        axis.set_title(label)
        axis.set_ylabel('Density')
        axis.legend(fontsize=8)
    plt.tight_layout()
    save_figure('fig_posteriors.png')

    figure, axes = plt.subplots(1, 2, figsize=(15, 5))
    figure.suptitle('Residual diagnostics for the pooled linear model', fontsize=15,
                    fontweight='bold')
    autocorrelation = np.array(residuals_test['autocorrelation'])
    axes[0].bar(range(1, len(autocorrelation)), autocorrelation[1:], color='steelblue')
    confidence_band = 1.96 / np.sqrt(len(predictions['test_target']))
    axes[0].axhline(confidence_band, color='red', linestyle='--', linewidth=1,
                    label='95% band under independence')
    axes[0].axhline(-confidence_band, color='red', linestyle='--', linewidth=1)
    axes[0].set_title('Residual autocorrelation (test)\nLjung-Box p = %.3g'
                      % residuals_test['ljung_box_p_value'])
    axes[0].set_xlabel('Lag')
    axes[0].set_ylabel('ACF')
    axes[0].legend(fontsize=9)
    test_residuals = predictions['test_target'] - predictions['gaussian_point_predictions']
    stats.probplot(test_residuals, dist='norm', plot=axes[1])
    axes[1].get_lines()[0].set_markersize(2)
    axes[1].set_title('Normal Q-Q plot of test residuals\nexcess kurtosis = %.0f'
                      % residuals_test['excess_kurtosis'])
    plt.tight_layout()
    save_figure('fig_residuals.png')

    figure, axes = plt.subplots(1, 2, figsize=(15, 5))
    figure.suptitle('Likelihood selection on the validation set', fontsize=15, fontweight='bold')
    student_candidates = [entry for entry in likelihood_candidates
                          if entry['likelihood'] == 'Student-t']
    gaussian_candidate = [entry for entry in likelihood_candidates
                          if entry['likelihood'] == 'Gaussian'][0]
    degrees = [entry['degrees_of_freedom'] for entry in student_candidates]
    axes[0].plot(degrees, [entry['validation_log_density'] for entry in student_candidates],
                 'o-', color='seagreen', linewidth=2, label='Student-t')
    axes[0].axhline(gaussian_candidate['validation_log_density'], color='coral',
                    linestyle='--', linewidth=2, label='Gaussian')
    axes[0].set_xscale('log')
    axes[0].set_xlabel('Degrees of freedom nu')
    axes[0].set_ylabel('Mean log predictive density (validation)')
    axes[0].set_title('Selection criterion')
    axes[0].legend()
    axes[1].plot(degrees, [entry['validation_coverage_50'] for entry in student_candidates],
                 'o-', color='seagreen', linewidth=2, label='Student-t, 50% interval')
    axes[1].axhline(gaussian_candidate['validation_coverage_50'], color='coral', linestyle='--',
                    linewidth=2, label='Gaussian, 50% interval')
    axes[1].axhline(0.5, color='black', linestyle=':', linewidth=1.5, label='nominal 50%')
    axes[1].set_xscale('log')
    axes[1].set_xlabel('Degrees of freedom nu')
    axes[1].set_ylabel('Validation coverage')
    axes[1].set_title('Calibration across the grid')
    axes[1].legend(fontsize=9)
    plt.tight_layout()
    save_figure('fig_nu_selection.png')

    figure, axes = plt.subplots(1, 2, figsize=(15, 5))
    figure.suptitle('Posterior predictive calibration on the test set', fontsize=15,
                    fontweight='bold')
    levels = ['50% interval', '95% interval']
    gaussian_coverage = [predictions['gaussian']['coverage_50'],
                         predictions['gaussian']['coverage_95']]
    gaussian_bounds = [predictions['gaussian']['coverage_50_interval'],
                       predictions['gaussian']['coverage_95_interval']]
    robust_coverage = [predictions['robust']['coverage_50'], predictions['robust']['coverage_95']]
    robust_bounds = [predictions['robust']['coverage_50_interval'],
                     predictions['robust']['coverage_95_interval']]
    positions = np.arange(2)
    gaussian_errors = np.array([[c - b[0] for c, b in zip(gaussian_coverage, gaussian_bounds)],
                                [b[1] - c for c, b in zip(gaussian_coverage, gaussian_bounds)]])
    robust_errors = np.array([[c - b[0] for c, b in zip(robust_coverage, robust_bounds)],
                              [b[1] - c for c, b in zip(robust_coverage, robust_bounds)]])
    axes[0].bar(positions - 0.2, gaussian_coverage, 0.4, yerr=gaussian_errors, capsize=6,
                label='Gaussian', color='coral', edgecolor='black')
    axes[0].bar(positions + 0.2, robust_coverage, 0.4, yerr=robust_errors, capsize=6,
                label='Student-t (nu=%.0f)' % predictions['selected_degrees_of_freedom'],
                color='seagreen', edgecolor='black')
    axes[0].plot(positions, [0.5, 0.95], 'k*', markersize=18, label='nominal level')
    axes[0].set_xticks(positions)
    axes[0].set_xticklabels(levels)
    axes[0].set_ylabel('Empirical coverage')
    axes[0].set_title('Coverage with block-bootstrap 95% intervals')
    axes[0].legend(fontsize=9)
    axes[1].bar(positions - 0.2, [predictions['gaussian']['width_50_percentage_points'],
                                  predictions['gaussian']['width_95_percentage_points']],
                0.4, label='Gaussian', color='coral', edgecolor='black')
    axes[1].bar(positions + 0.2, [predictions['robust']['width_50_percentage_points'],
                                  predictions['robust']['width_95_percentage_points']],
                0.4, label='Student-t', color='seagreen', edgecolor='black')
    axes[1].set_xticks(positions)
    axes[1].set_xticklabels(levels)
    axes[1].set_ylabel('Mean interval width (CPU percentage points)')
    axes[1].set_title('Interval width in original units')
    axes[1].legend(fontsize=9)
    plt.tight_layout()
    save_figure('fig_calibration.png')

    figure, axes = plt.subplots(1, 2, figsize=(16, 5.5))
    figure.suptitle('HMC sensitivity to step size and trajectory length', fontsize=15,
                    fontweight='bold')
    step_sizes = sorted({entry['step_size'] for entry in hmc_grid})
    leapfrog_values = sorted({entry['leapfrog_steps'] for entry in hmc_grid})
    efficiency = np.full((len(step_sizes), len(leapfrog_values)), np.nan)
    acceptance = np.full((len(step_sizes), len(leapfrog_values)), np.nan)
    for entry in hmc_grid:
        row = step_sizes.index(entry['step_size'])
        column = leapfrog_values.index(entry['leapfrog_steps'])
        efficiency[row, column] = entry['bulk_ess_per_gradient']
        acceptance[row, column] = entry['acceptance']
    for axis, matrix, title, formatter in [
            (axes[0], efficiency, 'Bulk ESS per gradient evaluation', '%.1e'),
            (axes[1], acceptance, 'Acceptance rate', '%.2f')]:
        image = axis.imshow(matrix, cmap='viridis', aspect='auto')
        axis.set_xticks(range(len(leapfrog_values)))
        axis.set_xticklabels(leapfrog_values)
        axis.set_yticks(range(len(step_sizes)))
        axis.set_yticklabels(step_sizes)
        axis.set_xlabel('Leapfrog steps L')
        axis.set_ylabel('Step size epsilon')
        axis.set_title(title)
        for row in range(len(step_sizes)):
            for column in range(len(leapfrog_values)):
                axis.text(column, row, formatter % matrix[row, column], ha='center',
                          va='center', color='white', fontsize=9)
        figure.colorbar(image, ax=axis, shrink=0.85)
    plt.tight_layout()
    save_figure('fig_hmc_sensitivity.png')

    sampler_names = list(initialisation.keys())
    figure, axes = plt.subplots(1, len(sampler_names), figsize=(6 * len(sampler_names), 5))
    figure.suptitle('Effect of the starting point on convergence', fontsize=15, fontweight='bold')
    for axis, sampler_name in zip(np.atleast_1d(axes), sampler_names):
        for start_name, entry in initialisation[sampler_name].items():
            axis.plot(entry['log_posterior_trace'], linewidth=1.5, label=start_name)
        axis.set_title(sampler_name)
        axis.set_xlabel('Iteration')
        axis.set_ylabel('Log posterior')
        axis.set_yscale('symlog')
        axis.legend(fontsize=8)
    plt.tight_layout()
    save_figure('fig_initialisation.png')

    figure, axis = plt.subplots(figsize=(14, 5.5))
    shown = min(250, len(predictions['test_target']))
    axis.plot(range(shown), predictions['test_target'][:shown] * dataset['target_std']
              + dataset['target_mean'], 'k-', linewidth=1.2, label='Actual')
    axis.plot(range(shown), predictions['gaussian_point_predictions'][:shown]
              * dataset['target_std'] + dataset['target_mean'], color='coral', linewidth=1.2,
              label='Predicted (Gaussian)')
    axis.fill_between(range(shown),
                      predictions['gaussian_lower_95'][:shown] * dataset['target_std']
                      + dataset['target_mean'],
                      predictions['gaussian_upper_95'][:shown] * dataset['target_std']
                      + dataset['target_mean'], color='coral', alpha=0.2,
                      label='95% posterior predictive interval')
    axis.set_xlabel('Test observation index')
    axis.set_ylabel('CPU usage (%)')
    axis.set_title('One-step-ahead forecasts on the test set, original units')
    axis.legend()
    plt.tight_layout()
    save_figure('fig_predictions.png')


# --------------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------------

def main():
    skip_nuts = '--skip-nuts' in sys.argv
    print('Loading data and building the forecasting design...')
    dataset = load_and_prepare_data()
    splits = dataset['splits']
    train, validation, test = splits['train'], splits['validation'], splits['test']
    print('  horizon %d minutes | train %d | validation %d | test %d | embargoed %d'
          % (dataset['horizon_minutes'], len(train['target']), len(validation['target']),
             len(test['target']), dataset['embargoed_count']))
    least_squares = np.linalg.lstsq(train['design'], train['target'], rcond=None)[0]
    residual_variance = float(np.mean((train['target'] - train['design'] @ least_squares) ** 2))
    print('\nResidual diagnostics on the test split...')
    residuals_test = residual_diagnostics(test['design'], test['target'], test['machine'],
                                          dataset['target_std'])
    print('  lag-1 ACF %.3f | Ljung-Box p %.3g | excess kurtosis %.1f'
          % (residuals_test['lag1_autocorrelation'], residuals_test['ljung_box_p_value'],
             residuals_test['excess_kurtosis']))
    pooling_analysis = residual_diagnostics(train['design'], train['target'], train['machine'],
                                            dataset['target_std'])
    print('  per-VM residual sd on train ranges %.3f to %.3f (median %.3f) across %d machines'
          % (pooling_analysis['per_machine_residual_sd_min'],
             pooling_analysis['per_machine_residual_sd_max'],
             pooling_analysis['per_machine_residual_sd_median'],
             pooling_analysis['per_machine_count']))
    geometry = posterior_geometry(train['design'], residual_variance)
    print('\nPosterior geometry: condition number %.1f, max |correlation| %.3f'
          % (geometry['condition_number'], geometry['max_absolute_correlation']))
    sampler_specifications = [('MH', metropolis_hastings),
                              ('Adaptive MH (naive)', adaptive_metropolis_hastings),
                              ('Preconditioned MH', preconditioned_metropolis_hastings),
                              ('Gibbs', gibbs_sampler),
                              ('HMC', hamiltonian_monte_carlo)]
    summaries, chain_storage = {}, {}
    for name, function in sampler_specifications:
        print('\nRunning %s: %d chains x %s draws, %d repeats...'
              % (name, CHAIN_COUNT, '{:,}'.format(POSTERIOR_DRAWS), REPEAT_COUNT))
        summary, coefficient_chains, variance_chains = run_sampler_repeatedly(
            function, train['design'], train['target'], name)
        summaries[name] = summary
        chain_storage[name] = {'coefficients': coefficient_chains, 'variances': variance_chains}
        print('  acceptance %.3f | R-hat %.4f | bulk ESS %.1f | tail ESS %.1f | %.1f s | %s'
              % (summary['acceptance_rate'], summary['worst_rhat'], summary['min_bulk_ess'],
                 summary['min_tail_ess'], summary['time'],
                 'converged' if summary['converged'] else 'NOT converged'))
    print('\nLonger run for Preconditioned MH (%s draws per chain)...'
          % '{:,}'.format(LONG_RUN_DRAWS))
    long_chains, long_variances, long_acceptances = [], [], []
    started = time.time()
    for chain_index in range(CHAIN_COUNT):
        output = preconditioned_metropolis_hastings(train['design'], train['target'],
                                                    LONG_RUN_DRAWS, BURN_IN_DRAWS,
                                                    random_seed=500 + chain_index)
        long_chains.append(output['coefficients'])
        long_variances.append(output['variances'])
        long_acceptances.append(output['acceptance_rate'])
    long_summary = summarise_sampler(long_chains, long_variances, time.time() - started,
                                     float(np.mean(long_acceptances)), 0, LONG_RUN_DRAWS)
    print('  R-hat %.4f | bulk ESS %.1f | %s' % (long_summary['worst_rhat'],
                                                 long_summary['min_bulk_ess'],
                                                 'converged' if long_summary['converged']
                                                 else 'still borderline'))
    print('\nAnalytical reference by quadrature...')
    analytical = analytical_posterior_reference(train['design'], train['target'])
    analytical['largest_deviation_from_gibbs'] = float(max(
        abs(a - b) for a, b in zip(analytical['posterior_mean_coefficients'],
                                   summaries['Gibbs']['posterior_mean_coefficients'])))
    print('  sigma^2 %.6f | largest deviation of Gibbs from the analytical mean %.6f'
          % (analytical['posterior_mean_variance'], analytical['largest_deviation_from_gibbs']))
    external = None
    try:
        print('\nExternal library reference (emcee)...')
        external = run_emcee_reference(train['design'], train['target'])
        external['largest_deviation_from_gibbs'] = float(max(
            abs(a - b) for a, b in zip(external['posterior_mean_coefficients'],
                                       summaries['Gibbs']['posterior_mean_coefficients'])))
        print('  %s | %s draws in %.1f s | worst R-hat %.4f | min bulk ESS %.0f | deviation %.5f'
              % (external['library'], '{:,}'.format(external['total_draws']), external['time'],
                 external['worst_rhat'], external['minimum_bulk_ess'],
                 external['largest_deviation_from_gibbs']))
    except Exception as error:
        print('  emcee reference failed: %s' % error)
    arviz_check = cross_check_against_arviz(
        [chain[:, 0] for chain in chain_storage['Gibbs']['coefficients']])
    if arviz_check:
        print('\nDiagnostic cross-check against ArviZ (Gibbs intercept):')
        print('  R-hat ours %.5f vs ArviZ %.5f | bulk ESS ours %.1f vs ArviZ %.1f'
              % (arviz_check['ours_rhat'], arviz_check['arviz_rhat'],
                 arviz_check['ours_bulk_ess'], arviz_check['arviz_bulk_ess']))
    print('\nSelecting the likelihood and nu on the validation split...')
    likelihood_candidates, selected = select_likelihood_on_validation(train, validation,
                                                                      dataset['target_std'])
    print('  selected: %s%s' % (selected['likelihood'],
                                '' if selected['degrees_of_freedom'] is None
                                else ' with nu = %.0f' % selected['degrees_of_freedom']))
    selected_degrees = selected['degrees_of_freedom'] if selected['likelihood'] == 'Student-t' \
        else STUDENT_T_GRID[2]
    print('\nFinal evaluation on the untouched test split...')
    gaussian_fit = gibbs_sampler(train['design'], train['target'], POSTERIOR_DRAWS,
                                 BURN_IN_DRAWS, random_seed=3)
    gaussian_test = posterior_predictive_evaluation(
        gaussian_fit['coefficients'], gaussian_fit['variances'], test['design'], test['target'],
        dataset['target_std'], random_seed=21)
    robust_fit = robust_student_t_gibbs(train['design'], train['target'], POSTERIOR_DRAWS,
                                        BURN_IN_DRAWS, selected_degrees, random_seed=3)
    robust_test = posterior_predictive_evaluation(
        robust_fit['coefficients'], robust_fit['variances'], test['design'], test['target'],
        dataset['target_std'], random_seed=21, degrees_of_freedom=selected_degrees)
    print('  Gaussian : RMSE %.4f (%.3f pp) | median abs err %.4f (%.3f pp) | 50%% cov %.3f'
          % (gaussian_test['rmse'], gaussian_test['rmse_percentage_points'],
             gaussian_test['median_absolute_error'],
             gaussian_test['median_absolute_error_percentage_points'],
             gaussian_test['coverage_50']))
    print('  Student-t: RMSE %.4f (%.3f pp) | median abs err %.4f (%.3f pp) | 50%% cov %.3f'
          % (robust_test['rmse'], robust_test['rmse_percentage_points'],
             robust_test['median_absolute_error'],
             robust_test['median_absolute_error_percentage_points'],
             robust_test['coverage_50']))
    ols_predictions = test['design'] @ least_squares
    ols_errors = test['target'] - ols_predictions
    ols_metrics = {'rmse': float(np.sqrt(np.mean(ols_errors ** 2))),
                   'median_absolute_error': float(np.median(np.abs(ols_errors))),
                   'rmse_percentage_points': float(np.sqrt(np.mean(ols_errors ** 2))
                                                   * dataset['target_std']),
                   'median_absolute_error_percentage_points': float(
                       np.median(np.abs(ols_errors)) * dataset['target_std'])}
    print('  OLS      : RMSE %.4f (%.3f pp) | median abs err %.4f (%.3f pp)'
          % (ols_metrics['rmse'], ols_metrics['rmse_percentage_points'],
             ols_metrics['median_absolute_error'],
             ols_metrics['median_absolute_error_percentage_points']))
    print('\nHMC sensitivity grid over step size and leapfrog steps...')
    warm_start = np.concatenate([least_squares, [np.log(residual_variance)]])
    hmc_grid = hamiltonian_sensitivity_study(train['design'], train['target'], warm_start)
    metropolis_grid = metropolis_sensitivity_study(train['design'], train['target'])
    print('\nInitialisation study...')
    initialisation = initialisation_study(train['design'], train['target'], least_squares)
    for sampler_name, entries in initialisation.items():
        for start_name, entry in entries.items():
            print('  %-18s from %-20s -> %d iterations'
                  % (sampler_name, start_name, entry['iterations_to_stationarity']))
    predictive_draws = np.quantile(
        (gaussian_fit['coefficients'][:2000] @ test['design'].T
         + np.sqrt(gaussian_fit['variances'][:2000])[:, None]
         * np.random.default_rng(5).normal(size=(2000, len(test['target'])))),
        [0.025, 0.975], axis=0)
    predictions = {'test_target': test['target'],
                   'gaussian_point_predictions': gaussian_test['point_predictions'],
                   'gaussian_lower_95': predictive_draws[0],
                   'gaussian_upper_95': predictive_draws[1],
                   'gaussian': gaussian_test, 'robust': robust_test,
                   'selected_degrees_of_freedom': selected_degrees}
    print('\nGenerating figures...')
    raw_sample = pd.concat([pd.read_csv(path, sep=';\t', header=0, engine='python').rename(
        columns=dict(zip(pd.read_csv(path, sep=';\t', header=0, engine='python').columns,
                         TELEMETRY_COLUMNS)))
        for path in sorted(glob.glob(os.path.join(DATA_DIRECTORY, '*.csv')))[:10]],
        ignore_index=True)
    generate_figures(chain_storage, summaries, residuals_test, geometry, initialisation,
                     likelihood_candidates, hmc_grid, predictions, dataset, raw_sample)
    payload = {
        'configuration': {
            'draws_per_chain': POSTERIOR_DRAWS, 'burn_in': BURN_IN_DRAWS,
            'chains': CHAIN_COUNT, 'repeats': REPEAT_COUNT,
            'long_run_draws': LONG_RUN_DRAWS,
            'rhat_threshold': RHAT_THRESHOLD, 'bulk_ess_threshold': BULK_ESS_THRESHOLD,
            'horizon_minutes': dataset['horizon_minutes'],
            'bootstrap_block_length': BOOTSTRAP_BLOCK_LENGTH,
            'bootstrap_replicates': BOOTSTRAP_REPLICATES,
            'student_t_grid': STUDENT_T_GRID},
        'data': {'n_train': int(len(train['target'])),
                 'n_validation': int(len(validation['target'])),
                 'n_test': int(len(test['target'])),
                 'n_features': int(train['design'].shape[1]),
                 'embargoed_count': dataset['embargoed_count'],
                 'total_available_rows': dataset['total_available'],
                 'distinct_machines': dataset['distinct_machines'],
                 'target_std': dataset['target_std'], 'target_mean': dataset['target_mean'],
                 'predictor_names': dataset['predictor_names']},
        'samplers': {name: {key: value for key, value in summary.items()}
                     for name, summary in summaries.items()},
        'preconditioned_long_run': {key: value for key, value in long_summary.items()},
        'analytical_reference': analytical,
        'external_reference': external,
        'arviz_cross_check': arviz_check,
        'likelihood_selection': {'candidates': likelihood_candidates, 'selected': selected},
        'test_performance': {
            'gaussian': {k: v for k, v in gaussian_test.items() if k != 'point_predictions'},
            'student_t': {k: v for k, v in robust_test.items() if k != 'point_predictions'},
            'ols': ols_metrics},
        'residual_diagnostics': residuals_test,
        'pooling_analysis': {key: value for key, value in pooling_analysis.items()
                             if key.startswith('per_machine') or key == 'residual_sd'},
        'posterior_geometry': {k: v for k, v in geometry.items()
                               if not isinstance(v, np.ndarray)},
        'hmc_sensitivity': hmc_grid,
        'mh_sensitivity': metropolis_grid,
        'initialisation_sensitivity': {
            sampler: {start: {'iterations_to_stationarity':
                              entry['iterations_to_stationarity'],
                              'stationary_level': entry['stationary_level']}
                      for start, entry in entries.items()}
            for sampler, entries in initialisation.items()},
    }
    with open(RESULTS_FILE, 'w') as handle:
        json.dump(payload, handle, indent=2)
    print('\nSaved experiment_results_v2.json')


if __name__ == '__main__':
    main()
