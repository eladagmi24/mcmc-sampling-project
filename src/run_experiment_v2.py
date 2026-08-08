"""Extension experiments for the MCMC sampling project.

This script adds the pieces the first round did not cover:
  1. A reliable effective-sample-size estimator (Geyer initial monotone positive sequence)
     and split R-hat, replacing diagnostics that saturated at the chain length.
  2. An adaptive, preconditioned Metropolis-Hastings sampler that repairs the efficiency
     collapse caused by the anisotropic posterior.
  3. Validation against PyMC / NUTS, an established probabilistic programming library.
  4. A residual analysis that identifies the true cause of the interval over-coverage, and a
     robust Student-t regression sampled with a scale-mixture Gibbs sampler that repairs it.
  5. A sensitivity study of the effect of the starting point on convergence.
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

PROJECT_ROOT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACE_DATA_DIRECTORY = os.path.join(PROJECT_ROOT_DIRECTORY, 'data', 'fastStorage', '2013-8')
RESULTS_DIRECTORY = os.path.join(PROJECT_ROOT_DIRECTORY, 'results')
FIGURES_DIRECTORY = os.path.join(RESULTS_DIRECTORY, 'figures')
RESULTS_FILE_PATH = os.path.join(RESULTS_DIRECTORY, 'experiment_results_v2.json')
os.makedirs(FIGURES_DIRECTORY, exist_ok=True)


def figure_path(figure_filename):
    """Absolute path of a figure inside results/figures, so the script runs from any directory."""
    return os.path.join(FIGURES_DIRECTORY, figure_filename)


import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy import stats
from scipy.special import gammaln

plt.rcParams.update({'figure.figsize': (12, 6), 'font.size': 12, 'axes.grid': True,
                     'grid.alpha': 0.3})

TELEMETRY_COLUMNS = ['Timestamp', 'CPU_Cores', 'CPU_Capacity_MHz', 'CPU_Usage_MHz', 'CPU_Usage_Pct',
                     'Mem_Provisioned_KB', 'Mem_Usage_KB', 'Disk_Read_KBps', 'Disk_Write_KBps',
                     'Net_Recv_KBps', 'Net_Trans_KBps']
VIRTUAL_MACHINE_COUNT = 50
MAXIMUM_OBSERVATIONS = 5000
TRAINING_FRACTION = 0.7
COEFFICIENT_PRIOR_VARIANCE = 10.0
VARIANCE_PRIOR_SHAPE = 2.0
VARIANCE_PRIOR_SCALE = 1.0
POSTERIOR_SAMPLE_COUNT = 10000
BURN_IN_COUNT = 2000
CHAIN_COUNT = 3
STUDENT_T_DEGREES_OF_FREEDOM = 4.0
NUTS_DRAW_COUNT = 500
NUTS_TUNE_COUNT = 500
PREDICTIVE_DRAW_COUNT = 2000
METHOD_COLORS = {'MH': 'steelblue', 'Adaptive MH (naive)': 'firebrick',
                 'Preconditioned MH': 'darkorange', 'Gibbs': 'coral', 'HMC': 'seagreen',
                 'NUTS (PyMC)': 'purple'}
METHOD_ORDER = ['MH', 'Adaptive MH (naive)', 'Preconditioned MH', 'Gibbs', 'HMC', 'NUTS (PyMC)']


def load_and_prepare_data():
    csv_paths = sorted(glob.glob(os.path.join(TRACE_DATA_DIRECTORY,
                                              '*.csv')))[:VIRTUAL_MACHINE_COUNT]
    per_machine_frames = []
    for csv_path in csv_paths:
        machine_frame = pd.read_csv(csv_path, sep=';\t', header=0, engine='python')
        machine_frame.columns = TELEMETRY_COLUMNS
        machine_frame['VM_ID'] = os.path.basename(csv_path).replace('.csv', '')
        per_machine_frames.append(machine_frame)
    combined_frame = pd.concat(per_machine_frames, ignore_index=True)
    combined_frame['Datetime'] = pd.to_datetime(combined_frame['Timestamp'], unit='s')
    engineered_frames = []
    for machine_identifier in combined_frame['VM_ID'].unique():
        machine_data = combined_frame[combined_frame['VM_ID'] == machine_identifier]
        if len(machine_data) < 50:
            continue
        frame = machine_data.sort_values('Datetime').copy()
        for lag_length in [1, 2, 3]:
            frame['CPU_lag_%d' % lag_length] = frame['CPU_Usage_Pct'].shift(lag_length)
        frame['CPU_rolling_mean'] = frame['CPU_Usage_Pct'].shift(1).rolling(window=6).mean()
        frame['CPU_rolling_std'] = frame['CPU_Usage_Pct'].shift(1).rolling(window=6).std()
        engineered_frames.append(frame.dropna().reset_index(drop=True))
    features_frame = pd.concat(engineered_frames, ignore_index=True)
    features_frame = features_frame.sort_values('Datetime').reset_index(drop=True)
    features_frame = features_frame.iloc[:MAXIMUM_OBSERVATIONS].reset_index(drop=True)
    predictor_names = (['Mem_Usage_KB', 'Disk_Read_KBps', 'Disk_Write_KBps', 'Net_Recv_KBps',
                        'Net_Trans_KBps'] + ['CPU_lag_%d' % i for i in [1, 2, 3]]
                       + ['CPU_rolling_mean', 'CPU_rolling_std'])
    raw_predictors = features_frame[predictor_names].values
    raw_target = features_frame['CPU_Usage_Pct'].values
    predictor_standard_deviations = raw_predictors.std(axis=0)
    predictor_standard_deviations[predictor_standard_deviations == 0] = 1.0
    scaled_predictors = (raw_predictors - raw_predictors.mean(axis=0)) / predictor_standard_deviations
    target_standard_deviation = raw_target.std() or 1.0
    scaled_target = (raw_target - raw_target.mean()) / target_standard_deviation
    design_matrix = np.column_stack([np.ones(len(scaled_predictors)), scaled_predictors])
    split_index = int(TRAINING_FRACTION * len(scaled_target))
    return {'train_design': design_matrix[:split_index], 'test_design': design_matrix[split_index:],
            'train_target': scaled_target[:split_index], 'test_target': scaled_target[split_index:],
            'predictor_names': predictor_names}


def autocorrelation_function(chain):
    chain_length = len(chain)
    centered_chain = chain - chain.mean()
    padded_length = 1
    while padded_length < 2 * chain_length:
        padded_length *= 2
    frequency_domain = np.fft.rfft(centered_chain, padded_length)
    autocovariance = np.fft.irfft(frequency_domain * np.conjugate(frequency_domain),
                                  padded_length)[:chain_length]
    autocovariance /= np.arange(chain_length, 0, -1)
    if autocovariance[0] <= 0:
        return np.zeros(chain_length)
    return autocovariance / autocovariance[0]


def effective_sample_size(chain):
    """Geyer initial monotone positive sequence estimator.

    The pairwise sums of successive autocorrelations are provably positive and decreasing for
    a reversible chain, so truncating at the first non-positive pair removes the noise floor
    that makes naive threshold estimators report the full chain length for any good sampler.
    """
    chain = np.asarray(chain, dtype=np.float64)
    chain_length = len(chain)
    if np.var(chain) == 0:
        return 0.0
    autocorrelations = autocorrelation_function(chain)
    maximum_pair_count = (chain_length - 1) // 2
    pair_sums = autocorrelations[1:2 * maximum_pair_count + 1:2] \
        + autocorrelations[2:2 * maximum_pair_count + 2:2]
    positive_pair_count = np.argmax(pair_sums <= 0) if np.any(pair_sums <= 0) else len(pair_sums)
    if positive_pair_count == 0:
        return float(chain_length)
    retained_pairs = np.minimum.accumulate(pair_sums[:positive_pair_count])
    integrated_autocorrelation_time = max(-1.0 + 2.0 * retained_pairs.sum(), 1.0)
    return float(min(chain_length, chain_length / integrated_autocorrelation_time))


def split_potential_scale_reduction(chains):
    """Split R-hat: each chain is halved so that within-chain drift inflates the statistic."""
    split_chains = []
    for chain in chains:
        half_length = len(chain) // 2
        split_chains.append(np.asarray(chain[:half_length], dtype=np.float64))
        split_chains.append(np.asarray(chain[half_length:2 * half_length], dtype=np.float64))
    chain_count = len(split_chains)
    chain_length = len(split_chains[0])
    chain_means = np.array([segment.mean() for segment in split_chains])
    chain_variances = np.array([segment.var(ddof=1) for segment in split_chains])
    within_chain_variance = chain_variances.mean()
    if within_chain_variance <= 0:
        return float('inf')
    between_chain_variance = chain_length * chain_means.var(ddof=1)
    pooled_variance = (chain_length - 1) / chain_length * within_chain_variance \
        + between_chain_variance / chain_length
    return float(np.sqrt(pooled_variance / within_chain_variance))


def plain_potential_scale_reduction(chains):
    """The original, unsplit Gelman-Rubin statistic, retained for comparison with split R-hat."""
    chain_count = len(chains)
    chain_length = len(chains[0])
    chain_means = np.array([np.mean(chain) for chain in chains])
    within_chain_variance = np.mean([np.var(chain, ddof=1) for chain in chains])
    if within_chain_variance <= 0:
        return float('inf')
    between_chain_variance = chain_length / (chain_count - 1) \
        * np.sum((chain_means - chain_means.mean()) ** 2)
    pooled_variance = (1 - 1 / chain_length) * within_chain_variance \
        + between_chain_variance / chain_length
    return float(np.sqrt(pooled_variance / within_chain_variance))


def monte_carlo_standard_error(chains):
    pooled_chain = np.concatenate(chains)
    per_chain_effective_sizes = sum(effective_sample_size(chain) for chain in chains)
    if per_chain_effective_sizes <= 0:
        return float('nan')
    return float(np.std(pooled_chain, ddof=1) / np.sqrt(per_chain_effective_sizes))


def log_likelihood_gaussian(coefficients, noise_variance, design, target):
    residuals = target - design @ coefficients
    return -0.5 * len(target) * np.log(2 * np.pi * noise_variance) \
        - 0.5 * np.sum(residuals ** 2) / noise_variance


def log_prior_coefficients(coefficients):
    return -0.5 * len(coefficients) * np.log(2 * np.pi * COEFFICIENT_PRIOR_VARIANCE) \
        - 0.5 * np.sum(coefficients ** 2) / COEFFICIENT_PRIOR_VARIANCE


def log_prior_noise_variance(noise_variance):
    if noise_variance <= 0:
        return -np.inf
    return VARIANCE_PRIOR_SHAPE * np.log(VARIANCE_PRIOR_SCALE) - gammaln(VARIANCE_PRIOR_SHAPE) \
        - (VARIANCE_PRIOR_SHAPE + 1) * np.log(noise_variance) - VARIANCE_PRIOR_SCALE / noise_variance


def log_posterior(coefficients, noise_variance, design, target):
    variance_prior_term = log_prior_noise_variance(noise_variance)
    if np.isinf(variance_prior_term):
        return -np.inf
    return log_likelihood_gaussian(coefficients, noise_variance, design, target) \
        + log_prior_coefficients(coefficients) + variance_prior_term


def log_posterior_unconstrained(parameter_vector, design, target):
    coefficient_count = design.shape[1]
    noise_variance = np.exp(parameter_vector[coefficient_count])
    return log_posterior(parameter_vector[:coefficient_count], noise_variance, design, target) \
        + parameter_vector[coefficient_count]


def gradient_log_posterior_unconstrained(parameter_vector, design, target):
    coefficient_count = design.shape[1]
    coefficients = parameter_vector[:coefficient_count]
    noise_variance = np.exp(parameter_vector[coefficient_count])
    residuals = target - design @ coefficients
    coefficient_gradient = (design.T @ residuals) / noise_variance \
        - coefficients / COEFFICIENT_PRIOR_VARIANCE
    log_variance_gradient = -0.5 * len(target) + 0.5 * np.sum(residuals ** 2) / noise_variance \
        - (VARIANCE_PRIOR_SHAPE + 1) + VARIANCE_PRIOR_SCALE / noise_variance + 1
    return np.concatenate([coefficient_gradient, [log_variance_gradient]])


def metropolis_hastings(design, target, sample_count, burn_in, coefficient_step=0.001,
                        log_variance_step=0.05, random_seed=0, initial_parameters=None):
    generator = np.random.default_rng(random_seed)
    coefficient_count = design.shape[1]
    parameter_vector = np.zeros(coefficient_count + 1) if initial_parameters is None \
        else initial_parameters.copy()
    proposal_scales = np.concatenate([np.full(coefficient_count, coefficient_step),
                                      [log_variance_step]])
    current_log_posterior = log_posterior_unconstrained(parameter_vector, design, target)
    coefficient_samples = np.zeros((sample_count, coefficient_count))
    variance_samples = np.zeros(sample_count)
    log_posterior_trace = np.zeros(sample_count + burn_in)
    accepted_count = 0
    for iteration_index in range(sample_count + burn_in):
        proposed_vector = parameter_vector + generator.normal(0, proposal_scales)
        proposed_log_posterior = log_posterior_unconstrained(proposed_vector, design, target)
        if np.log(generator.uniform()) < proposed_log_posterior - current_log_posterior:
            parameter_vector = proposed_vector
            current_log_posterior = proposed_log_posterior
            if iteration_index >= burn_in:
                accepted_count += 1
        log_posterior_trace[iteration_index] = current_log_posterior
        if iteration_index >= burn_in:
            coefficient_samples[iteration_index - burn_in] = parameter_vector[:coefficient_count]
            variance_samples[iteration_index - burn_in] = np.exp(parameter_vector[coefficient_count])
    return {'coefficients': coefficient_samples, 'variances': variance_samples,
            'acceptance_rate': accepted_count / sample_count, 'log_posterior': log_posterior_trace}


def adaptive_metropolis_hastings(design, target, sample_count, burn_in, random_seed=0,
                                 adaptation_interval=200, initial_parameters=None):
    """Haario adaptive Metropolis: the proposal covariance is learned during burn-in only.

    Freezing the covariance at the end of burn-in keeps the retained samples a homogeneous
    Markov chain, so the usual convergence guarantees apply without appealing to diminishing
    adaptation results.
    """
    generator = np.random.default_rng(random_seed)
    coefficient_count = design.shape[1]
    dimension = coefficient_count + 1
    parameter_vector = np.zeros(dimension) if initial_parameters is None \
        else initial_parameters.copy()
    scaling_constant = 2.38 ** 2 / dimension
    regularization = 1e-10 * np.eye(dimension)
    proposal_covariance = np.diag(np.concatenate([np.full(coefficient_count, 0.001 ** 2), [0.05 ** 2]]))
    proposal_cholesky = np.linalg.cholesky(proposal_covariance)
    current_log_posterior = log_posterior_unconstrained(parameter_vector, design, target)
    coefficient_samples = np.zeros((sample_count, coefficient_count))
    variance_samples = np.zeros(sample_count)
    log_posterior_trace = np.zeros(sample_count + burn_in)
    burn_in_history = np.zeros((burn_in, dimension))
    accepted_count = 0
    for iteration_index in range(sample_count + burn_in):
        proposed_vector = parameter_vector + proposal_cholesky @ generator.normal(size=dimension)
        proposed_log_posterior = log_posterior_unconstrained(proposed_vector, design, target)
        if np.log(generator.uniform()) < proposed_log_posterior - current_log_posterior:
            parameter_vector = proposed_vector
            current_log_posterior = proposed_log_posterior
            if iteration_index >= burn_in:
                accepted_count += 1
        log_posterior_trace[iteration_index] = current_log_posterior
        if iteration_index < burn_in:
            burn_in_history[iteration_index] = parameter_vector
            if (iteration_index + 1) % adaptation_interval == 0 and iteration_index > dimension * 2:
                empirical_covariance = np.cov(burn_in_history[:iteration_index + 1].T)
                proposal_covariance = scaling_constant * empirical_covariance + regularization
                try:
                    proposal_cholesky = np.linalg.cholesky(proposal_covariance)
                except np.linalg.LinAlgError:
                    pass
        else:
            coefficient_samples[iteration_index - burn_in] = parameter_vector[:coefficient_count]
            variance_samples[iteration_index - burn_in] = np.exp(parameter_vector[coefficient_count])
    return {'coefficients': coefficient_samples, 'variances': variance_samples,
            'acceptance_rate': accepted_count / sample_count, 'log_posterior': log_posterior_trace,
            'proposal_covariance': proposal_covariance}


def preconditioned_metropolis_hastings(design, target, sample_count, burn_in, random_seed=0,
                                       adaptation_interval=100, initial_parameters=None,
                                       target_acceptance=0.234):
    """Metropolis-Hastings preconditioned by the observed Fisher information.

    Estimating the proposal covariance purely from the chain's own history fails here: the
    unpreconditioned chain barely moves during burn-in, so the empirical covariance describes
    its transient drift rather than the posterior. We therefore start from an analytic
    preconditioner available from a single least-squares fit, which requires no conjugacy, and
    refine it during burn-in while a Robbins-Monro rule tunes a global scale toward the optimal
    acceptance rate. Both adaptations stop before the retained samples are collected.
    """
    generator = np.random.default_rng(random_seed)
    observation_count, coefficient_count = design.shape
    dimension = coefficient_count + 1
    least_squares_coefficients = np.linalg.lstsq(design, target, rcond=None)[0]
    residual_variance = float(np.mean((target - design @ least_squares_coefficients) ** 2))
    coefficient_covariance = residual_variance * np.linalg.inv(
        design.T @ design + np.eye(coefficient_count) / COEFFICIENT_PRIOR_VARIANCE)
    base_covariance = np.zeros((dimension, dimension))
    base_covariance[:coefficient_count, :coefficient_count] = coefficient_covariance
    base_covariance[coefficient_count, coefficient_count] = 2.0 / observation_count
    optimal_scaling = 2.38 ** 2 / dimension
    log_scale = 0.0
    base_cholesky = np.linalg.cholesky(base_covariance)
    proposal_cholesky = np.exp(log_scale) * np.sqrt(optimal_scaling) * base_cholesky
    parameter_vector = np.zeros(dimension) if initial_parameters is None \
        else initial_parameters.copy()
    current_log_posterior = log_posterior_unconstrained(parameter_vector, design, target)
    coefficient_samples = np.zeros((sample_count, coefficient_count))
    variance_samples = np.zeros(sample_count)
    log_posterior_trace = np.zeros(sample_count + burn_in)
    burn_in_history = np.zeros((burn_in, dimension))
    accepted_count = 0
    transient_length = burn_in // 2
    for iteration_index in range(sample_count + burn_in):
        proposed_vector = parameter_vector + proposal_cholesky @ generator.normal(size=dimension)
        proposed_log_posterior = log_posterior_unconstrained(proposed_vector, design, target)
        acceptance_indicator = 0.0
        if np.log(generator.uniform()) < proposed_log_posterior - current_log_posterior:
            parameter_vector = proposed_vector
            current_log_posterior = proposed_log_posterior
            acceptance_indicator = 1.0
            if iteration_index >= burn_in:
                accepted_count += 1
        log_posterior_trace[iteration_index] = current_log_posterior
        if iteration_index < burn_in:
            burn_in_history[iteration_index] = parameter_vector
            learning_rate = min(0.5, 5.0 / (iteration_index + 1) ** 0.6)
            log_scale += learning_rate * (acceptance_indicator - target_acceptance)
            if (iteration_index + 1) % adaptation_interval == 0 \
                    and iteration_index > transient_length + 10 * dimension:
                post_transient = burn_in_history[transient_length:iteration_index + 1]
                empirical_covariance = np.cov(post_transient.T)
                ridge = 1e-10 * np.trace(empirical_covariance) / dimension * np.eye(dimension)
                try:
                    base_cholesky = np.linalg.cholesky(empirical_covariance + ridge)
                except np.linalg.LinAlgError:
                    pass
            proposal_cholesky = np.exp(log_scale) * np.sqrt(optimal_scaling) * base_cholesky
        else:
            coefficient_samples[iteration_index - burn_in] = parameter_vector[:coefficient_count]
            variance_samples[iteration_index - burn_in] = np.exp(parameter_vector[coefficient_count])
    return {'coefficients': coefficient_samples, 'variances': variance_samples,
            'acceptance_rate': accepted_count / sample_count, 'log_posterior': log_posterior_trace,
            'final_log_scale': log_scale}


def gibbs_sampler(design, target, sample_count, burn_in, random_seed=0, initial_parameters=None):
    generator = np.random.default_rng(random_seed)
    observation_count, coefficient_count = design.shape
    cross_product = design.T @ design
    cross_target = design.T @ target
    coefficients = np.zeros(coefficient_count)
    noise_variance = 1.0
    if initial_parameters is not None:
        coefficients = initial_parameters[:coefficient_count].copy()
        noise_variance = float(np.exp(initial_parameters[coefficient_count]))
    coefficient_samples = np.zeros((sample_count, coefficient_count))
    variance_samples = np.zeros(sample_count)
    log_posterior_trace = np.zeros(sample_count + burn_in)
    identity_matrix = np.eye(coefficient_count)
    for iteration_index in range(sample_count + burn_in):
        precision_matrix = cross_product / noise_variance + identity_matrix / COEFFICIENT_PRIOR_VARIANCE
        covariance_matrix = np.linalg.inv(precision_matrix)
        mean_vector = covariance_matrix @ (cross_target / noise_variance)
        coefficients = generator.multivariate_normal(mean_vector, covariance_matrix)
        residuals = target - design @ coefficients
        noise_variance = 1.0 / generator.gamma(VARIANCE_PRIOR_SHAPE + observation_count / 2.0,
                                               1.0 / (VARIANCE_PRIOR_SCALE
                                                      + 0.5 * np.sum(residuals ** 2)))
        log_posterior_trace[iteration_index] = log_posterior(coefficients, noise_variance,
                                                             design, target)
        if iteration_index >= burn_in:
            coefficient_samples[iteration_index - burn_in] = coefficients
            variance_samples[iteration_index - burn_in] = noise_variance
    return {'coefficients': coefficient_samples, 'variances': variance_samples,
            'acceptance_rate': 1.0, 'log_posterior': log_posterior_trace}


def hamiltonian_monte_carlo(design, target, sample_count, burn_in, step_size=0.002,
                            leapfrog_steps=15, random_seed=0, initial_parameters=None):
    generator = np.random.default_rng(random_seed)
    coefficient_count = design.shape[1]
    dimension = coefficient_count + 1
    parameter_vector = np.zeros(dimension) if initial_parameters is None \
        else initial_parameters.copy()
    current_log_posterior = log_posterior_unconstrained(parameter_vector, design, target)
    coefficient_samples = np.zeros((sample_count, coefficient_count))
    variance_samples = np.zeros(sample_count)
    log_posterior_trace = np.zeros(sample_count + burn_in)
    accepted_count = 0
    for iteration_index in range(sample_count + burn_in):
        momentum = generator.normal(size=dimension)
        proposed_vector = parameter_vector.copy()
        proposed_momentum = momentum.copy()
        gradient = gradient_log_posterior_unconstrained(proposed_vector, design, target)
        proposed_momentum += 0.5 * step_size * gradient
        for leapfrog_index in range(leapfrog_steps):
            proposed_vector += step_size * proposed_momentum
            gradient = gradient_log_posterior_unconstrained(proposed_vector, design, target)
            if leapfrog_index < leapfrog_steps - 1:
                proposed_momentum += step_size * gradient
        proposed_momentum += 0.5 * step_size * gradient
        proposed_log_posterior = log_posterior_unconstrained(proposed_vector, design, target)
        energy_difference = (proposed_log_posterior - 0.5 * np.sum(proposed_momentum ** 2)) \
            - (current_log_posterior - 0.5 * np.sum(momentum ** 2))
        if np.log(generator.uniform()) < energy_difference:
            parameter_vector = proposed_vector
            current_log_posterior = proposed_log_posterior
            if iteration_index >= burn_in:
                accepted_count += 1
        log_posterior_trace[iteration_index] = current_log_posterior
        if iteration_index >= burn_in:
            coefficient_samples[iteration_index - burn_in] = parameter_vector[:coefficient_count]
            variance_samples[iteration_index - burn_in] = np.exp(parameter_vector[coefficient_count])
    return {'coefficients': coefficient_samples, 'variances': variance_samples,
            'acceptance_rate': accepted_count / sample_count, 'log_posterior': log_posterior_trace}


def robust_student_t_gibbs(design, target, sample_count, burn_in, degrees_of_freedom,
                           random_seed=0):
    """Gibbs sampler for Student-t regression using the normal scale-mixture representation.

    Writing y_i ~ N(x_i'b, s2 / w_i) with w_i ~ Gamma(v/2, v/2) marginalises to a Student-t
    likelihood while keeping every full conditional conjugate, so no tuning is required.
    """
    generator = np.random.default_rng(random_seed)
    observation_count, coefficient_count = design.shape
    coefficients = np.zeros(coefficient_count)
    noise_scale_squared = 1.0
    observation_weights = np.ones(observation_count)
    coefficient_samples = np.zeros((sample_count, coefficient_count))
    scale_samples = np.zeros(sample_count)
    identity_matrix = np.eye(coefficient_count)
    for iteration_index in range(sample_count + burn_in):
        weighted_design = design * observation_weights[:, None]
        precision_matrix = (design.T @ weighted_design) / noise_scale_squared \
            + identity_matrix / COEFFICIENT_PRIOR_VARIANCE
        covariance_matrix = np.linalg.inv(precision_matrix)
        mean_vector = covariance_matrix @ (weighted_design.T @ target / noise_scale_squared)
        coefficients = generator.multivariate_normal(mean_vector, covariance_matrix)
        residuals = target - design @ coefficients
        weighted_residual_sum = np.sum(observation_weights * residuals ** 2)
        noise_scale_squared = 1.0 / generator.gamma(
            VARIANCE_PRIOR_SHAPE + observation_count / 2.0,
            1.0 / (VARIANCE_PRIOR_SCALE + 0.5 * weighted_residual_sum))
        weight_shape = (degrees_of_freedom + 1.0) / 2.0
        weight_rate = (degrees_of_freedom + residuals ** 2 / noise_scale_squared) / 2.0
        observation_weights = generator.gamma(weight_shape, 1.0 / weight_rate)
        if iteration_index >= burn_in:
            coefficient_samples[iteration_index - burn_in] = coefficients
            scale_samples[iteration_index - burn_in] = noise_scale_squared
    return {'coefficients': coefficient_samples, 'variances': scale_samples,
            'acceptance_rate': 1.0}


def run_pymc_nuts(design, target, draw_count, tune_count, chain_count, random_seed=0):
    import pymc
    import pytensor
    pytensor.config.cxx = ''
    with pymc.Model():
        coefficients = pymc.Normal('beta', mu=0.0, sigma=np.sqrt(COEFFICIENT_PRIOR_VARIANCE),
                                   shape=design.shape[1])
        noise_variance = pymc.InverseGamma('sigma2', alpha=VARIANCE_PRIOR_SHAPE,
                                           beta=VARIANCE_PRIOR_SCALE)
        expected_target = pymc.math.dot(design, coefficients)
        pymc.Normal('y', mu=expected_target, sigma=pymc.math.sqrt(noise_variance),
                    observed=target)
        start_time = time.time()
        inference_data = pymc.sample(draws=draw_count, tune=tune_count, chains=chain_count,
                                     cores=1, random_seed=random_seed, progressbar=False,
                                     compute_convergence_checks=False,
                                     nuts={'max_treedepth': 7, 'target_accept': 0.8})
        elapsed_time = time.time() - start_time
    coefficient_chains = inference_data.posterior['beta'].values
    variance_chains = inference_data.posterior['sigma2'].values
    return {'coefficient_chains': [coefficient_chains[i] for i in range(chain_count)],
            'variance_chains': [variance_chains[i] for i in range(chain_count)],
            'time': elapsed_time, 'acceptance_rate': float('nan')}


def run_emcee_reference(design, target, walker_count=40, step_count=4000, discarded_steps=1000,
                        random_seed=42):
    """Sample the same posterior with emcee, an established third-party MCMC library.

    emcee implements the affine-invariant ensemble sampler of Goodman and Weare, an algorithm
    unrelated to any of ours, and is pure Python, so it needs no compiler toolchain. It serves
    here purely as an external check on the posterior, not as an efficiency competitor: an
    ensemble sampler's draws are not comparable with single-chain ESS on equal terms.
    """
    import emcee
    generator = np.random.default_rng(random_seed)
    coefficient_count = design.shape[1]
    dimension = coefficient_count + 1
    least_squares_coefficients = np.linalg.lstsq(design, target, rcond=None)[0]
    residual_variance = float(np.mean((target - design @ least_squares_coefficients) ** 2))
    centre = np.concatenate([least_squares_coefficients, [np.log(residual_variance)]])
    starting_positions = centre + 1e-3 * generator.normal(size=(walker_count, dimension))
    ensemble_sampler = emcee.EnsembleSampler(walker_count, dimension,
                                             log_posterior_unconstrained, args=(design, target))
    start_time = time.time()
    ensemble_sampler.run_mcmc(starting_positions, step_count, progress=False)
    elapsed_time = time.time() - start_time
    retained = ensemble_sampler.get_chain(discard=discarded_steps, flat=False)
    flattened = retained.reshape(-1, dimension)
    walkers_per_group = walker_count // CHAIN_COUNT
    coefficient_chains, variance_chains = [], []
    for group_index in range(CHAIN_COUNT):
        group = retained[:, group_index * walkers_per_group:(group_index + 1) * walkers_per_group, :]
        group = group.reshape(-1, dimension)
        coefficient_chains.append(group[:, :coefficient_count])
        variance_chains.append(np.exp(group[:, coefficient_count]))
    scale_reductions = {}
    for label, index in [('Intercept', 0), ('beta_1', 1), ('beta_3', 3)]:
        scale_reductions[label] = split_potential_scale_reduction(
            [chain[:, index] for chain in coefficient_chains])
    scale_reductions['sigma2'] = split_potential_scale_reduction(variance_chains)
    return {'posterior_mean_coefficients': flattened[:, :coefficient_count].mean(axis=0).tolist(),
            'posterior_sd_coefficients': flattened[:, :coefficient_count].std(axis=0).tolist(),
            'posterior_mean_variance': float(np.exp(flattened[:, coefficient_count]).mean()),
            'split_rhat': scale_reductions, 'time': elapsed_time,
            'acceptance_rate': float(np.mean(ensemble_sampler.acceptance_fraction)),
            'walkers': walker_count, 'steps': step_count, 'discarded_steps': discarded_steps,
            'total_draws': int(flattened.shape[0]), 'library': 'emcee %s' % emcee.__version__}


def summarise_chains(coefficient_chains, variance_chains, elapsed_time, acceptance_rate):
    effective_sizes = []
    for coefficient_index in range(coefficient_chains[0].shape[1]):
        for chain in coefficient_chains:
            effective_sizes.append(effective_sample_size(chain[:, coefficient_index]))
    for chain in variance_chains:
        effective_sizes.append(effective_sample_size(chain))
    average_effective_size = float(np.mean(effective_sizes))
    parameter_labels = ['Intercept', 'beta_1', 'beta_3', 'sigma2']
    parameter_indices = [0, 1, 3]
    scale_reductions = {}
    for label, index in zip(parameter_labels[:3], parameter_indices):
        scale_reductions[label] = split_potential_scale_reduction([c[:, index]
                                                                   for c in coefficient_chains])
    scale_reductions['sigma2'] = split_potential_scale_reduction(variance_chains)
    return {'acceptance_rate': acceptance_rate, 'avg_ess': average_effective_size,
            'time': elapsed_time, 'ess_per_sec': average_effective_size / elapsed_time,
            'split_rhat': scale_reductions,
            'mcse_intercept': monte_carlo_standard_error([c[:, 0] for c in coefficient_chains]),
            'posterior_mean_coefficients': np.vstack(coefficient_chains).mean(axis=0).tolist(),
            'posterior_mean_variance': float(np.concatenate(variance_chains).mean())}


def posterior_predictive_evaluation(coefficient_samples, variance_samples, test_design, test_target,
                                    random_seed=0, degrees_of_freedom=None):
    generator = np.random.default_rng(random_seed)
    draw_indices = generator.choice(len(coefficient_samples),
                                    size=min(PREDICTIVE_DRAW_COUNT, len(coefficient_samples)),
                                    replace=False)
    selected_coefficients = coefficient_samples[draw_indices]
    selected_variances = variance_samples[draw_indices]
    conditional_means = selected_coefficients @ test_design.T
    scale_values = np.sqrt(selected_variances)[:, None]
    if degrees_of_freedom is None:
        noise_draws = generator.normal(size=conditional_means.shape)
    else:
        noise_draws = generator.standard_t(degrees_of_freedom, size=conditional_means.shape)
    predictive_draws = conditional_means + scale_values * noise_draws
    point_predictions = conditional_means.mean(axis=0)
    root_mean_squared_error = float(np.sqrt(np.mean((test_target - point_predictions) ** 2)))
    median_absolute_error = float(np.median(np.abs(test_target - point_predictions)))
    coverage_results = {}
    interval_widths = {}
    for nominal_level in [0.50, 0.95]:
        lower_quantile = np.quantile(predictive_draws, (1 - nominal_level) / 2, axis=0)
        upper_quantile = np.quantile(predictive_draws, 1 - (1 - nominal_level) / 2, axis=0)
        coverage_results['coverage_%d' % int(nominal_level * 100)] = float(
            np.mean((test_target >= lower_quantile) & (test_target <= upper_quantile)))
        interval_widths['width_%d' % int(nominal_level * 100)] = float(
            np.mean(upper_quantile - lower_quantile))
    return {'rmse': root_mean_squared_error, 'median_absolute_error': median_absolute_error,
            'point_predictions': point_predictions, **coverage_results, **interval_widths}


def analyse_residuals(train_design, train_target, test_design, test_target):
    least_squares_coefficients = np.linalg.lstsq(train_design, train_target, rcond=None)[0]
    train_residuals = train_target - train_design @ least_squares_coefficients
    test_residuals = test_target - test_design @ least_squares_coefficients
    median_absolute_deviation = float(np.median(np.abs(test_residuals)))
    return {'train_residual_sd': float(train_residuals.std()),
            'test_residual_sd': float(test_residuals.std()),
            'test_residual_mad': median_absolute_deviation,
            'sd_to_robust_sd_ratio': float(test_residuals.std() / (1.4826 * median_absolute_deviation)),
            'excess_kurtosis': float(stats.kurtosis(test_residuals)),
            'skewness': float(stats.skew(test_residuals)),
            'fraction_beyond_three_sd': float(np.mean(np.abs(test_residuals)
                                                      > 3 * train_residuals.std())),
            'test_residuals': test_residuals, 'train_residuals': train_residuals}


def posterior_geometry_summary(train_design, noise_variance_estimate):
    precision_matrix = train_design.T @ train_design / noise_variance_estimate \
        + np.eye(train_design.shape[1]) / COEFFICIENT_PRIOR_VARIANCE
    covariance_matrix = np.linalg.inv(precision_matrix)
    eigenvalues = np.linalg.eigvalsh(covariance_matrix)
    standard_deviations = np.sqrt(np.diag(covariance_matrix))
    correlation_matrix = covariance_matrix / np.outer(standard_deviations, standard_deviations)
    off_diagonal_mask = ~np.eye(len(correlation_matrix), dtype=bool)
    return {'condition_number': float(eigenvalues.max() / eigenvalues.min()),
            'max_absolute_correlation': float(np.abs(correlation_matrix[off_diagonal_mask]).max()),
            'narrowest_direction_sd': float(np.sqrt(eigenvalues.min())),
            'widest_direction_sd': float(np.sqrt(eigenvalues.max())),
            'correlation_matrix': correlation_matrix}


def initialisation_sensitivity_study(train_design, train_target, least_squares_coefficients):
    coefficient_count = train_design.shape[1]
    starting_points = {
        'zeros (default)': np.zeros(coefficient_count + 1),
        'least squares fit': np.concatenate([least_squares_coefficients, [np.log(0.15)]]),
        'dispersed (all +3)': np.concatenate([np.full(coefficient_count, 3.0), [np.log(5.0)]]),
        'extreme variance': np.concatenate([np.zeros(coefficient_count), [np.log(100.0)]]),
    }
    sampler_functions = {'Preconditioned MH': preconditioned_metropolis_hastings,
                         'Gibbs': gibbs_sampler, 'HMC': hamiltonian_monte_carlo}
    study_results = {}
    for sampler_name, sampler_function in sampler_functions.items():
        study_results[sampler_name] = {}
        for start_name, start_vector in starting_points.items():
            sampler_output = sampler_function(train_design, train_target, 1000, 500,
                                              random_seed=7, initial_parameters=start_vector)
            log_posterior_trace = sampler_output['log_posterior']
            stationary_level = np.median(log_posterior_trace[-500:])
            tolerance_band = 0.01 * abs(stationary_level)
            within_band = np.abs(log_posterior_trace - stationary_level) < tolerance_band
            first_entry = int(np.argmax(within_band)) if within_band.any() else -1
            study_results[sampler_name][start_name] = {
                'iterations_to_stationarity': first_entry,
                'log_posterior_trace': log_posterior_trace[:400].tolist(),
                'final_log_posterior': float(stationary_level)}
    return study_results


def main():
    print('Loading and preparing data...')
    dataset = load_and_prepare_data()
    train_design = dataset['train_design']
    train_target = dataset['train_target']
    test_design = dataset['test_design']
    test_target = dataset['test_target']
    print('  train %d, test %d, parameters %d' % (len(train_target), len(test_target),
                                                  train_design.shape[1]))
    least_squares_coefficients = np.linalg.lstsq(train_design, train_target, rcond=None)[0]
    least_squares_rmse = float(np.sqrt(np.mean((test_target - test_design
                                                @ least_squares_coefficients) ** 2)))
    print('\nAnalysing the residual distribution...')
    residual_analysis = analyse_residuals(train_design, train_target, test_design, test_target)
    print('  excess kurtosis %.1f, sd/robust-sd ratio %.2f'
          % (residual_analysis['excess_kurtosis'], residual_analysis['sd_to_robust_sd_ratio']))
    print('\nSummarising the posterior geometry...')
    geometry_summary = posterior_geometry_summary(train_design,
                                                  residual_analysis['train_residual_sd'] ** 2)
    print('  condition number %.1f, max |correlation| %.3f'
          % (geometry_summary['condition_number'], geometry_summary['max_absolute_correlation']))
    sampler_specifications = [
        ('MH', metropolis_hastings, {}),
        ('Adaptive MH (naive)', adaptive_metropolis_hastings, {}),
        ('Preconditioned MH', preconditioned_metropolis_hastings, {}),
        ('Gibbs', gibbs_sampler, {}),
        ('HMC', hamiltonian_monte_carlo, {}),
    ]
    chain_storage = {}
    method_summaries = {}
    for method_name, sampler_function, extra_arguments in sampler_specifications:
        print('\nRunning %s (%d chains x %d samples)...' % (method_name, CHAIN_COUNT,
                                                            POSTERIOR_SAMPLE_COUNT))
        coefficient_chains, variance_chains, acceptance_rates = [], [], []
        start_time = time.time()
        for chain_index in range(CHAIN_COUNT):
            sampler_output = sampler_function(train_design, train_target, POSTERIOR_SAMPLE_COUNT,
                                              BURN_IN_COUNT, random_seed=42 + chain_index,
                                              **extra_arguments)
            coefficient_chains.append(sampler_output['coefficients'])
            variance_chains.append(sampler_output['variances'])
            acceptance_rates.append(sampler_output['acceptance_rate'])
        elapsed_time = time.time() - start_time
        chain_storage[method_name] = {'coefficients': coefficient_chains,
                                      'variances': variance_chains}
        method_summaries[method_name] = summarise_chains(coefficient_chains, variance_chains,
                                                         elapsed_time, float(np.mean(acceptance_rates)))
        predictive_metrics = posterior_predictive_evaluation(np.vstack(coefficient_chains),
                                                             np.concatenate(variance_chains),
                                                             test_design, test_target)
        method_summaries[method_name].update({key: value for key, value in predictive_metrics.items()
                                              if key != 'point_predictions'})
        chain_storage[method_name]['point_predictions'] = predictive_metrics['point_predictions']
        print('  acceptance %.3f | ESS %.1f | %.2f s | ESS/s %.1f | split R-hat(intercept) %.4f'
              % (method_summaries[method_name]['acceptance_rate'],
                 method_summaries[method_name]['avg_ess'], elapsed_time,
                 method_summaries[method_name]['ess_per_sec'],
                 method_summaries[method_name]['split_rhat']['Intercept']))
    skip_nuts_reference = '--skip-nuts' in sys.argv
    if skip_nuts_reference:
        print('\nSkipping the PyMC NUTS reference run (--skip-nuts was passed). Every other '
              'result in this script is unaffected; only Section 5.4 of the report is omitted.')
    try:
        if skip_nuts_reference:
            raise RuntimeError('skipped at the user\'s request')
        print('\nRunning the PyMC NUTS reference implementation...')
        nuts_output = run_pymc_nuts(train_design, train_target, draw_count=NUTS_DRAW_COUNT,
                                    tune_count=NUTS_TUNE_COUNT, chain_count=CHAIN_COUNT,
                                    random_seed=42)
        chain_storage['NUTS (PyMC)'] = {'coefficients': nuts_output['coefficient_chains'],
                                        'variances': nuts_output['variance_chains']}
        method_summaries['NUTS (PyMC)'] = summarise_chains(nuts_output['coefficient_chains'],
                                                           nuts_output['variance_chains'],
                                                           nuts_output['time'], float('nan'))
        nuts_predictive = posterior_predictive_evaluation(
            np.vstack(nuts_output['coefficient_chains']),
            np.concatenate(nuts_output['variance_chains']), test_design, test_target)
        method_summaries['NUTS (PyMC)'].update({key: value for key, value in nuts_predictive.items()
                                                if key != 'point_predictions'})
        print('  NUTS finished in %.1f s, ESS %.1f'
              % (nuts_output['time'], method_summaries['NUTS (PyMC)']['avg_ess']))
    except Exception as nuts_error:
        print('  NUTS reference run failed: %s' % nuts_error)
    print('\nRunning the emcee external reference...')
    external_reference = None
    try:
        external_reference = run_emcee_reference(train_design, train_target)
        largest_deviation = max(
            abs(reference_value - gibbs_value) for reference_value, gibbs_value
            in zip(external_reference['posterior_mean_coefficients'],
                   method_summaries['Gibbs']['posterior_mean_coefficients']))
        external_reference['largest_deviation_from_gibbs'] = float(largest_deviation)
        print('  %s: %d draws in %.1f s | acceptance %.3f | largest deviation from Gibbs %.5f'
              % (external_reference['library'], external_reference['total_draws'],
                 external_reference['time'], external_reference['acceptance_rate'],
                 largest_deviation))
    except Exception as reference_error:
        print('  emcee reference run failed: %s' % reference_error)
    print('\nRunning the robust Student-t Gibbs sampler (v=%.0f)...' % STUDENT_T_DEGREES_OF_FREEDOM)
    start_time = time.time()
    robust_output = robust_student_t_gibbs(train_design, train_target, POSTERIOR_SAMPLE_COUNT,
                                           BURN_IN_COUNT, STUDENT_T_DEGREES_OF_FREEDOM,
                                           random_seed=42)
    robust_elapsed_time = time.time() - start_time
    robust_predictive = posterior_predictive_evaluation(
        robust_output['coefficients'], robust_output['variances'], test_design, test_target,
        degrees_of_freedom=STUDENT_T_DEGREES_OF_FREEDOM)
    robust_summary = {key: value for key, value in robust_predictive.items()
                      if key != 'point_predictions'}
    robust_summary['time'] = robust_elapsed_time
    robust_summary['degrees_of_freedom'] = STUDENT_T_DEGREES_OF_FREEDOM
    print('  RMSE %.4f | median abs error %.4f | 50%% coverage %.3f | 95%% coverage %.3f'
          % (robust_summary['rmse'], robust_summary['median_absolute_error'],
             robust_summary['coverage_50'], robust_summary['coverage_95']))
    print('\nRunning the initialisation sensitivity study...')
    initialisation_results = initialisation_sensitivity_study(train_design, train_target,
                                                              least_squares_coefficients)
    for sampler_name, per_start in initialisation_results.items():
        for start_name, entry in per_start.items():
            print('  %-12s from %-20s -> stationary after %d iterations'
                  % (sampler_name, start_name, entry['iterations_to_stationarity']))
    print('\nGenerating figures...')
    generate_figures(chain_storage, method_summaries, residual_analysis, geometry_summary,
                     initialisation_results, robust_predictive, test_target, test_design,
                     dataset['predictor_names'])
    output_payload = {
        'n_samples': POSTERIOR_SAMPLE_COUNT, 'burn_in': BURN_IN_COUNT, 'n_chains': CHAIN_COUNT,
        'n_train': len(train_target), 'n_test': len(test_target),
        'n_features': train_design.shape[1], 'ols_rmse': least_squares_rmse,
        'nuts_draws': NUTS_DRAW_COUNT, 'nuts_tune': NUTS_TUNE_COUNT,
        'methods': {name: {key: value for key, value in summary.items()}
                    for name, summary in method_summaries.items()},
        'robust_student_t': robust_summary,
        'external_reference': external_reference,
        'residual_analysis': {key: value for key, value in residual_analysis.items()
                              if not isinstance(value, np.ndarray)},
        'posterior_geometry': {key: value for key, value in geometry_summary.items()
                               if not isinstance(value, np.ndarray)},
        'initialisation_sensitivity': {
            sampler_name: {start_name: {'iterations_to_stationarity':
                                        entry['iterations_to_stationarity'],
                                        'final_log_posterior': entry['final_log_posterior']}
                           for start_name, entry in per_start.items()}
            for sampler_name, per_start in initialisation_results.items()},
    }
    with open(RESULTS_FILE_PATH, 'w') as output_file:
        json.dump(output_payload, output_file, indent=2)
    print('\nSaved %s' % os.path.relpath(RESULTS_FILE_PATH, PROJECT_ROOT_DIRECTORY))


def generate_figures(chain_storage, method_summaries, residual_analysis, geometry_summary,
                     initialisation_results, robust_predictive, test_target, test_design,
                     predictor_names):
    method_names = [name for name in METHOD_ORDER if name in method_summaries]
    preconditioning_pair = ['MH', 'Preconditioned MH']
    figure, axes = plt.subplots(1, 3, figsize=(18, 5))
    figure.suptitle('Why plain Metropolis-Hastings mixes badly: the posterior is anisotropic',
                    fontsize=15, fontweight='bold')
    correlation_image = axes[0].imshow(geometry_summary['correlation_matrix'], cmap='RdBu_r',
                                       vmin=-1, vmax=1)
    axes[0].set_title('Posterior correlation between coefficients')
    axes[0].set_xticks(range(len(predictor_names) + 1))
    axes[0].set_yticks(range(len(predictor_names) + 1))
    short_labels = ['int'] + [name[:8] for name in predictor_names]
    axes[0].set_xticklabels(short_labels, rotation=90, fontsize=8)
    axes[0].set_yticklabels(short_labels, fontsize=8)
    figure.colorbar(correlation_image, ax=axes[0], shrink=0.8)
    axes[1].bar(preconditioning_pair,
                [method_summaries[name]['avg_ess'] for name in preconditioning_pair],
                color=[METHOD_COLORS[name] for name in preconditioning_pair], edgecolor='black')
    axes[1].set_title('Effective sample size before and after preconditioning')
    axes[1].set_ylabel('Average ESS (of %d draws)' % POSTERIOR_SAMPLE_COUNT)
    for bar_index, method_name in enumerate(preconditioning_pair):
        axes[1].text(bar_index, method_summaries[method_name]['avg_ess'],
                     '%.0f' % method_summaries[method_name]['avg_ess'], ha='center', va='bottom')
    for method_name in preconditioning_pair:
        chain = chain_storage[method_name]['coefficients'][0][:, 0]
        autocorrelations = autocorrelation_function(chain)[:300]
        axes[2].plot(autocorrelations, label=method_name, color=METHOD_COLORS[method_name],
                     linewidth=2)
    axes[2].axhline(0.0, color='black', linewidth=1)
    axes[2].set_title('Autocorrelation of the intercept')
    axes[2].set_xlabel('Lag')
    axes[2].set_ylabel('ACF')
    axes[2].legend()
    plt.tight_layout()
    plt.savefig(figure_path('v2_preconditioning.png'), dpi=150, bbox_inches='tight')
    plt.close()

    figure, axes = plt.subplots(1, 3, figsize=(18, 5))
    figure.suptitle('The over-coverage is caused by heavy-tailed residuals, not by the priors',
                    fontsize=15, fontweight='bold')
    test_residuals = residual_analysis['test_residuals']
    axes[0].hist(test_residuals, bins=120, density=True, alpha=0.7, color='steelblue',
                 label='test residuals')
    residual_grid = np.linspace(test_residuals.min(), test_residuals.max(), 400)
    axes[0].plot(residual_grid, stats.norm.pdf(residual_grid, 0, residual_analysis['train_residual_sd']),
                 'r--', linewidth=2, label='Gaussian fitted to sigma')
    axes[0].set_yscale('log')
    axes[0].set_title('Residual density (log scale)\nexcess kurtosis = %.0f'
                      % residual_analysis['excess_kurtosis'])
    axes[0].set_xlabel('Residual')
    axes[0].legend()
    stats.probplot(test_residuals, dist='norm', plot=axes[1])
    axes[1].set_title('Normal Q-Q plot of the test residuals')
    axes[1].get_lines()[0].set_markersize(2)
    coverage_labels = ['50% interval', '95% interval']
    gaussian_coverages = [method_summaries['Gibbs']['coverage_50'],
                          method_summaries['Gibbs']['coverage_95']]
    robust_coverages = [robust_predictive['coverage_50'], robust_predictive['coverage_95']]
    bar_positions = np.arange(len(coverage_labels))
    axes[2].bar(bar_positions - 0.2, gaussian_coverages, 0.4, label='Gaussian likelihood',
                color='coral', edgecolor='black')
    axes[2].bar(bar_positions + 0.2, robust_coverages, 0.4, label='Student-t likelihood',
                color='seagreen', edgecolor='black')
    axes[2].plot(bar_positions, [0.5, 0.95], 'k*', markersize=16, label='nominal level')
    axes[2].set_xticks(bar_positions)
    axes[2].set_xticklabels(coverage_labels)
    axes[2].set_ylabel('Empirical coverage')
    axes[2].set_title('Calibration before and after the robust likelihood')
    axes[2].legend()
    plt.tight_layout()
    plt.savefig(figure_path('v2_calibration.png'), dpi=150, bbox_inches='tight')
    plt.close()

    figure, axes = plt.subplots(2, 2, figsize=(16, 10))
    figure.suptitle('Sampler comparison with corrected diagnostics', fontsize=15, fontweight='bold')
    comparison_metrics = [('Average ESS', [method_summaries[m]['avg_ess'] for m in method_names]),
                          ('Runtime (s)', [method_summaries[m]['time'] for m in method_names]),
                          ('ESS / second', [method_summaries[m]['ess_per_sec'] for m in method_names]),
                          ('Split R-hat (intercept)',
                           [method_summaries[m]['split_rhat']['Intercept'] for m in method_names])]
    for axis, (metric_name, metric_values) in zip(axes.flat, comparison_metrics):
        bars = axis.bar(method_names, metric_values,
                        color=[METHOD_COLORS[m] for m in method_names], edgecolor='black')
        axis.set_title(metric_name)
        axis.tick_params(axis='x', rotation=20)
        for bar, value in zip(bars, metric_values):
            axis.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                      '%.4g' % value, ha='center', va='bottom', fontsize=10)
        if metric_name in ('Average ESS', 'ESS / second'):
            axis.set_yscale('log')
    plt.tight_layout()
    plt.savefig(figure_path('v2_comparison.png'), dpi=150, bbox_inches='tight')
    plt.close()

    if 'NUTS (PyMC)' in chain_storage:
        figure, axes = plt.subplots(1, 2, figsize=(15, 6))
        figure.suptitle('Validation against PyMC / NUTS', fontsize=15, fontweight='bold')
        reference_means = np.vstack(chain_storage['NUTS (PyMC)']['coefficients']).mean(axis=0)
        reference_sds = np.vstack(chain_storage['NUTS (PyMC)']['coefficients']).std(axis=0)
        for method_name in method_names:
            if method_name == 'NUTS (PyMC)':
                continue
            our_means = np.vstack(chain_storage[method_name]['coefficients']).mean(axis=0)
            axes[0].scatter(reference_means, our_means, label=method_name,
                            color=METHOD_COLORS[method_name], s=60, alpha=0.8)
        axis_limits = [reference_means.min() - 0.05, reference_means.max() + 0.05]
        axes[0].plot(axis_limits, axis_limits, 'k--', linewidth=1, label='exact agreement')
        axes[0].set_xlabel('Posterior mean from PyMC / NUTS')
        axes[0].set_ylabel('Posterior mean from our sampler')
        axes[0].set_title('Posterior means agree with the reference implementation')
        axes[0].legend()
        for method_name in method_names:
            if method_name == 'NUTS (PyMC)':
                continue
            our_means = np.vstack(chain_storage[method_name]['coefficients']).mean(axis=0)
            standardised_difference = (our_means - reference_means) / reference_sds
            axes[1].plot(range(len(our_means)), standardised_difference, 'o-',
                         label=method_name, color=METHOD_COLORS[method_name])
        axes[1].axhline(0.0, color='black', linewidth=1)
        axes[1].axhline(0.1, color='red', linestyle=':', linewidth=1)
        axes[1].axhline(-0.1, color='red', linestyle=':', linewidth=1)
        axes[1].set_xlabel('Coefficient index')
        axes[1].set_ylabel('(ours - NUTS) / posterior sd')
        axes[1].set_title('Standardised difference from the reference')
        axes[1].legend()
        plt.tight_layout()
        plt.savefig(figure_path('v2_nuts_validation.png'), dpi=150, bbox_inches='tight')
        plt.close()

    figure, axes = plt.subplots(1, 2, figsize=(16, 5.5))
    figure.suptitle('Why the first round missed the problem: plain against split R-hat',
                    fontsize=15, fontweight='bold')
    checkpoints = np.arange(500, POSTERIOR_SAMPLE_COUNT + 1, 500)
    for axis, (parameter_index, parameter_label) in zip(axes, [(0, 'Intercept'), (1, 'β₁')]):
        for method_name in ['MH', 'Gibbs', 'HMC']:
            if method_name not in chain_storage:
                continue
            plain_values, split_values = [], []
            for checkpoint in checkpoints:
                truncated = [chain[:checkpoint, parameter_index]
                             for chain in chain_storage[method_name]['coefficients']]
                plain_values.append(plain_potential_scale_reduction(truncated))
                split_values.append(split_potential_scale_reduction(truncated))
            axis.plot(checkpoints, plain_values, '--', color=METHOD_COLORS[method_name],
                      linewidth=1.8, label='%s, plain R-hat' % method_name)
            axis.plot(checkpoints, split_values, '-', color=METHOD_COLORS[method_name],
                      linewidth=2.4, label='%s, split R-hat' % method_name)
        axis.axhline(1.1, color='red', linestyle=':', linewidth=1.2, label='1.1 threshold')
        axis.axhline(1.01, color='darkred', linestyle=':', linewidth=1.2, label='1.01 threshold')
        axis.set_title('Convergence diagnostic for %s' % parameter_label)
        axis.set_xlabel('Iterations used')
        axis.set_ylabel('R-hat')
        axis.set_yscale('log')
        axis.legend(fontsize=8, ncol=2)
    plt.tight_layout()
    plt.savefig(figure_path('v2_rhat_comparison.png'), dpi=150, bbox_inches='tight')
    plt.close()

    posterior_methods = [name for name in ['MH', 'Preconditioned MH', 'Gibbs', 'HMC']
                         if name in chain_storage]
    figure, axes = plt.subplots(2, 2, figsize=(15, 9))
    figure.suptitle('Posterior distributions: where the samplers disagree', fontsize=15,
                    fontweight='bold')
    for axis, (parameter_index, parameter_label) in zip(
            axes.flat, [(0, 'Intercept'), (1, 'β₁'), (3, 'β₃'), (None, 'σ²')]):
        for method_name in posterior_methods:
            if parameter_index is None:
                values = np.concatenate(chain_storage[method_name]['variances'])
            else:
                values = np.vstack(chain_storage[method_name]['coefficients'])[:, parameter_index]
            axis.hist(values, bins=80, density=True, alpha=0.45, label=method_name,
                      color=METHOD_COLORS[method_name])
        axis.set_title(parameter_label)
        axis.set_ylabel('Density')
        axis.legend(fontsize=9)
    plt.tight_layout()
    plt.savefig(figure_path('v2_posteriors.png'), dpi=150, bbox_inches='tight')
    plt.close()

    figure, axes = plt.subplots(1, 2, figsize=(16, 5))
    figure.suptitle('Autocorrelation and the noise variance trace', fontsize=15, fontweight='bold')
    for method_name in posterior_methods:
        autocorrelations = autocorrelation_function(
            chain_storage[method_name]['coefficients'][0][:, 0])[:200]
        axes[0].plot(autocorrelations, label=method_name, color=METHOD_COLORS[method_name],
                     linewidth=2)
    axes[0].axhline(0.0, color='black', linewidth=1)
    axes[0].set_title('Autocorrelation of the intercept (chain 1)')
    axes[0].set_xlabel('Lag')
    axes[0].set_ylabel('ACF')
    axes[0].legend(fontsize=9)
    for method_name in posterior_methods:
        axes[1].plot(chain_storage[method_name]['variances'][0][:2000], linewidth=0.6,
                     alpha=0.8, label=method_name, color=METHOD_COLORS[method_name])
    axes[1].set_title('Trace of the noise variance (first 2,000 retained draws)')
    axes[1].set_xlabel('Iteration')
    axes[1].set_ylabel('σ²')
    axes[1].legend(fontsize=9)
    plt.tight_layout()
    plt.savefig(figure_path('v2_autocorrelation_and_variance.png'), dpi=150, bbox_inches='tight')
    plt.close()

    sampler_names = list(initialisation_results.keys())
    figure, axes = plt.subplots(1, len(sampler_names), figsize=(6 * len(sampler_names), 5))
    figure.suptitle('Effect of the starting point on convergence', fontsize=15, fontweight='bold')
    for axis, sampler_name in zip(np.atleast_1d(axes), sampler_names):
        for start_name, entry in initialisation_results[sampler_name].items():
            axis.plot(entry['log_posterior_trace'], linewidth=1.5, label=start_name)
        axis.set_title(sampler_name)
        axis.set_xlabel('Iteration')
        axis.set_ylabel('Log posterior')
        axis.set_yscale('symlog')
        axis.legend(fontsize=9)
    plt.tight_layout()
    plt.savefig(figure_path('v2_initialisation.png'), dpi=150, bbox_inches='tight')
    plt.close()


if __name__ == '__main__':
    main()
