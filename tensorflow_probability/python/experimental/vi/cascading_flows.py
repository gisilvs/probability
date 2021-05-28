# Copyright 2021 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Utilities for constructing structured surrogate posteriors."""

from __future__ import absolute_import
from __future__ import division
# [internal] enable type annotations
from __future__ import print_function

import copy
import functools
import inspect

import tensorflow.compat.v2 as tf

from tensorflow_probability.python.bijectors import chain
from tensorflow_probability.python.bijectors import reshape
from tensorflow_probability.python.bijectors import scale as scale_lib
from tensorflow_probability.python.bijectors import shift
from tensorflow_probability.python.bijectors import split
from tensorflow_probability.python.distributions import batch_broadcast
from tensorflow_probability.python.distributions import beta
from tensorflow_probability.python.distributions import blockwise
from tensorflow_probability.python.distributions import chi2
from tensorflow_probability.python.distributions import deterministic
from tensorflow_probability.python.distributions import exponential
from tensorflow_probability.python.distributions import gamma
from tensorflow_probability.python.distributions import half_normal
from tensorflow_probability.python.distributions import independent
from tensorflow_probability.python.distributions import \
  joint_distribution_auto_batched
from tensorflow_probability.python.distributions import \
  joint_distribution_coroutine
from tensorflow_probability.python.distributions import normal
from tensorflow_probability.python.distributions import sample
from tensorflow_probability.python.distributions import transformed_distribution
from tensorflow_probability.python.distributions import truncated_normal
from tensorflow_probability.python.distributions import uniform
from tensorflow_probability.python.experimental.bijectors import \
  build_highway_flow_layer
from tensorflow_probability.python.internal import samplers

__all__ = [
  'register_cf_substitution_rule',
  'build_cf_surrogate_posterior'
]

Root = joint_distribution_coroutine.JointDistributionCoroutine.Root

_NON_STATISTICAL_PARAMS = [
  'name', 'validate_args', 'allow_nan_stats', 'experimental_use_kahan_sum',
  'reinterpreted_batch_ndims', 'dtype', 'force_probs_to_zero_outside_support',
  'num_probit_terms_approx'
]
_NON_TRAINABLE_PARAMS = ['low', 'high']

# Registry of transformations that are applied to distributions in the prior
# before defining the surrogate family.


# Todo: inherited from asvi code, do we need this?
ASVI_SURROGATE_SUBSTITUTIONS = {}


# Todo: inherited from asvi code, do we need this?
def _as_substituted_distribution(distribution):
  """Applies all substitution rules that match a distribution."""
  for condition, substitution_fn in ASVI_SURROGATE_SUBSTITUTIONS.items():
    if condition(distribution):
      distribution = substitution_fn(distribution)
  return distribution


# Todo: inherited from asvi code, do we need this?
def register_cf_substitution_rule(condition, substitution_fn):
  """Registers a rule for substituting distributions in ASVI surrogates.

  Args:
    condition: Python `callable` that takes a Distribution instance and
      returns a Python `bool` indicating whether or not to substitute it.
      May also be a class type such as `tfd.Normal`, in which case the
      condition is interpreted as
      `lambda distribution: isinstance(distribution, class)`.
    substitution_fn: Python `callable` that takes a Distribution
      instance and returns a new Distribution instance used to define
      the ASVI surrogate posterior. Note that this substitution does not modify
      the original model.

  #### Example

  To use a Normal surrogate for all location-scale family distributions, we
  could register the substitution:

  ```python
  tfp.experimental.vi.register_asvi_surrogate_substitution(
    condition=lambda distribution: (
      hasattr(distribution, 'loc') and hasattr(distribution, 'scale'))
    substitution_fn=lambda distribution: (
      # Invoking the event space bijector applies any relevant constraints,
      # e.g., that HalfCauchy samples must be `>= loc`.
      distribution.experimental_default_event_space_bijector()(
        tfd.Normal(loc=distribution.loc, scale=distribution.scale)))
  ```

  This rule will fire when ASVI encounters a location-scale distribution,
  and instructs ASVI to build a surrogate 'as if' the model had just used a
  (possibly constrained) Normal in its place. Note that we could have used a
  more precise condition, e.g., to limit the substitution to distributions with
  a specific `name`, if we had reason to think that a Normal distribution would
  be a good surrogate for some model variables but not others.

  """
  global ASVI_SURROGATE_SUBSTITUTIONS
  if inspect.isclass(condition):
    condition = lambda distribution, cls=condition: isinstance(
      # pylint: disable=g-long-lambda
      distribution, cls)
  ASVI_SURROGATE_SUBSTITUTIONS[condition] = substitution_fn


# Default substitutions attempt to express distributions using the most
# flexible available parameterization.
# pylint: disable=g-long-lambda
register_cf_substitution_rule(
  half_normal.HalfNormal,
  lambda dist: truncated_normal.TruncatedNormal(
    loc=0., scale=dist.scale, low=0., high=dist.scale * 10.))
register_cf_substitution_rule(
  uniform.Uniform,
  lambda dist: shift.Shift(dist.low)(
    scale_lib.Scale(dist.high - dist.low)(
      beta.Beta(concentration0=tf.ones_like(dist.mean()),
                concentration1=1.))))
register_cf_substitution_rule(
  exponential.Exponential,
  lambda dist: gamma.Gamma(concentration=1., rate=dist.rate))
register_cf_substitution_rule(
  chi2.Chi2,
  lambda dist: gamma.Gamma(concentration=0.5 * dist.df, rate=0.5))


# pylint: enable=g-long-lambda

# a single JointDistribution.
def build_cf_surrogate_posterior(
    prior,
    num_auxiliary_variables=0,
    initial_prior_weight=0.5,
    seed=None,
    name=None):
  # todo: change docstrings
  """Builds a structured surrogate posterior inspired by conjugate updating.

  ASVI, or Automatic Structured Variational Inference, was proposed by
  Ambrogioni et al. (2020) [1] as a method of automatically constructing a
  surrogate posterior with the same structure as the prior. It does this by
  reparameterizing the variational family of the surrogate posterior by
  structuring each parameter according to the equation
  ```none
  prior_weight * prior_parameter + (1 - prior_weight) * mean_field_parameter
  ```
  In this equation, `prior_parameter` is a vector of prior parameters and
  `mean_field_parameter` is a vector of trainable parameters with the same
  domain as `prior_parameter`. `prior_weight` is a vector of learnable
  parameters where `0. <= prior_weight <= 1.`. When `prior_weight =
  0`, the surrogate posterior will be a mean-field surrogate, and when
  `prior_weight = 1.`, the surrogate posterior will be the prior. This convex
  combination equation, inspired by conjugacy in exponential families, thus
  allows the surrogate posterior to balance between the structure of the prior
  and the structure of a mean-field approximation.

  Args:
    prior: tfd.JointDistribution instance of the prior.
    mean_field: Optional Python boolean. If `True`, creates a degenerate
      surrogate distribution in which all variables are independent,
      ignoring the prior dependence structure. Default value: `False`.
    initial_prior_weight: Optional float value (either static or tensor value)
      on the interval [0, 1]. A larger value creates an initial surrogate
      distribution with more dependence on the prior structure. Default value:
      `0.5`.
    seed: Python `int` seed for random initialization.
    name: Optional string. Default value: `build_cf_surrogate_posterior`.

  Returns:
    surrogate_posterior: A `tfd.JointDistributionCoroutineAutoBatched` instance
    whose samples have shape and structure matching that of `prior`.

  Raises:
    TypeError: The `prior` argument cannot be a nested `JointDistribution`.

  ### Examples

  Consider a Brownian motion model expressed as a JointDistribution:

  ```python
  prior_loc = 0.
  innovation_noise = .1

  def model_fn():
    new = yield tfd.Normal(loc=prior_loc, scale=innovation_noise)
    for i in range(4):
      new = yield tfd.Normal(loc=new, scale=innovation_noise)

  prior = tfd.JointDistributionCoroutineAutoBatched(model_fn)
  ```

  Let's use variational inference to approximate the posterior. We'll build a
  surrogate posterior distribution by feeding in the prior distribution.

  ```python
  surrogate_posterior =
    tfp.experimental.vi.build_cf_surrogate_posterior(prior)
  ```

  This creates a trainable joint distribution, defined by variables in
  `surrogate_posterior.trainable_variables`. We use `fit_surrogate_posterior`
  to fit this distribution by minimizing a divergence to the true posterior.

  ```python
  losses = tfp.vi.fit_surrogate_posterior(
    target_log_prob_fn,
    surrogate_posterior=surrogate_posterior,
    num_steps=100,
    optimizer=tf.optimizers.Adam(0.1),
    sample_size=10)

  # After optimization, samples from the surrogate will approximate
  # samples from the true posterior.
  samples = surrogate_posterior.sample(100)
  posterior_mean = [tf.reduce_mean(x) for x in samples]
  posterior_std = [tf.math.reduce_std(x) for x in samples]
  ```

  #### References
  [1]: Luca Ambrogioni, Max Hinne, Marcel van Gerven. Automatic structured
        variational inference. _arXiv preprint arXiv:2002.00643_, 2020
        https://arxiv.org/abs/2002.00643

  """
  with tf.name_scope(name or 'build_cf_surrogate_posterior'):
    surrogate_posterior, variables = _cf_surrogate_for_distribution(
      dist=prior,
      base_distribution_surrogate_fn=functools.partial(
        _cf_convex_update_for_base_distribution,
        initial_prior_weight=initial_prior_weight,
        num_auxiliary_variables=num_auxiliary_variables),
      num_auxiliary_variables=num_auxiliary_variables,
      seed=seed)
    surrogate_posterior.also_track = variables
    return surrogate_posterior


def _cf_surrogate_for_distribution(dist,
                                   base_distribution_surrogate_fn,
                                   sample_shape=None,
                                   variables=None,
                                   num_auxiliary_variables=0,
                                   global_auxiliary_variables=None,
                                   seed=None):
  # todo: change docstrings
  """Recursively creates ASVI surrogates, and creates new variables if needed.

  Args:
    dist: a `tfd.Distribution` instance.
    base_distribution_surrogate_fn: Callable to build a surrogate posterior
      for a 'base' (non-meta and non-joint) distribution, with signature
      `surrogate_posterior, variables = base_distribution_fn(
      dist, sample_shape=None, variables=None, seed=None)`.
    sample_shape: Optional `Tensor` shape of samples drawn from `dist` by
      `tfd.Sample` wrappers. If not `None`, the surrogate's event will include
      independent sample dimensions, i.e., it will have event shape
      `concat([sample_shape, dist.event_shape], axis=0)`.
      Default value: `None`.
    variables: Optional nested structure of `tf.Variable`s returned from a
      previous call to `_cf_surrogate_for_distribution`. If `None`,
      new variables will be created; otherwise, constructs a surrogate posterior
      backed by the passed-in variables.
      Default value: `None`.
    seed: Python `int` seed for random initialization.
  Returns:
    surrogate_posterior: Instance of `tfd.Distribution` representing a trainable
      surrogate posterior distribution, with the same structure and `name` as
      `dist`.
    variables: Nested structure of `tf.Variable` trainable parameters for the
      surrogate posterior. If `dist` is a base distribution, this is
      a `dict` of `ASVIParameters` instances. If `dist` is a joint
      distribution, this is a `dist.dtype` structure of such `dict`s.
  """

  # Apply any substitutions, while attempting to preserve the original name.
  dist = _set_name(_as_substituted_distribution(dist), name=_get_name(dist))

  if hasattr(dist, '_model_coroutine'):
    surrogate_posterior, variables = _cf_surrogate_for_joint_distribution(
      dist,
      base_distribution_surrogate_fn=base_distribution_surrogate_fn,
      variables=variables,
      num_auxiliary_variables=num_auxiliary_variables,
      global_auxiliary_variables=global_auxiliary_variables,
      seed=seed)
  else:
    surrogate_posterior, variables = base_distribution_surrogate_fn(
      dist=dist, sample_shape=sample_shape, variables=variables,
      global_auxiliary_variables=global_auxiliary_variables, seed=seed)
  return surrogate_posterior, variables


def _cf_surrogate_for_joint_distribution(
    dist, base_distribution_surrogate_fn, variables=None,
    num_auxiliary_variables=0, global_auxiliary_variables=None, seed=None):
  """Builds a structured joint surrogate posterior for a joint model."""

  # Probabilistic program for CF surrogate posterior.
  flat_variables = dist._model_flatten(
    variables) if variables else None  # pylint: disable=protected-access
  prior_coroutine = dist._model_coroutine  # pylint: disable=protected-access

  def posterior_generator(seed=seed):
    prior_gen = prior_coroutine()
    dist = next(prior_gen)

    if num_auxiliary_variables > 0:
      i = 1

      if flat_variables:
        variables = flat_variables[0]

      else:
        layers = 3
        bijectors = []

        for _ in range(0, layers - 1):
          bijectors.append(
            build_highway_flow_layer(num_auxiliary_variables,
                                     residual_fraction_initial_value=0.5,
                                     activation_fn=True, gate_first_n=0,
                                     seed=seed))
        bijectors.append(
          build_highway_flow_layer(num_auxiliary_variables,
                                   residual_fraction_initial_value=0.5,
                                   activation_fn=False, gate_first_n=0,
                                   seed=seed))

        variables = chain.Chain(bijectors=list(reversed(bijectors)))

      eps = transformed_distribution.TransformedDistribution(
        distribution=sample.Sample(normal.Normal(0., 0.1),
                                   num_auxiliary_variables),
        bijector=variables)

      eps = Root(eps)

      value_out = yield (eps if flat_variables
                         else (eps, variables))

      global_auxiliary_variables = value_out

    else:
      i = 0

    try:
      while True:
        was_root = isinstance(dist, Root)
        if was_root:
          dist = dist.distribution

        seed, init_seed = samplers.split_seed(seed)
        surrogate_posterior, variables = _cf_surrogate_for_distribution(
          dist,
          base_distribution_surrogate_fn=base_distribution_surrogate_fn,
          variables=flat_variables[i] if flat_variables else None,
          global_auxiliary_variables=global_auxiliary_variables,
          seed=init_seed)

        if was_root and num_auxiliary_variables == 0:
          surrogate_posterior = Root(surrogate_posterior)
        # If variables were not given---i.e., we're creating new
        # variables---then yield the new variables along with the surrogate
        # posterior. This assumes an execution context such as
        # `_extract_variables_from_coroutine_model` below that will capture and
        # save the variables.
        value_out = yield (surrogate_posterior if flat_variables
                           else (surrogate_posterior, variables))
        if type(value_out) == list:
          if len(dist.event_shape) == 0:
            dist = prior_gen.send(tf.squeeze(value_out[0], -1))
          else:
            dist = prior_gen.send(value_out[0])

        else:
          dist = prior_gen.send(value_out)
        i += 1
    except StopIteration:
      pass

  if variables is None:
    # Run the generator to create variables, then call ourselves again
    # to construct the surrogate JD from these variables. Note that we can't
    # just create a JDC from the current `posterior_generator`, because it will
    # try to build new variables on every invocation; the recursive call will
    # define a new `posterior_generator` that knows about the variables we're
    # about to create.
    return _cf_surrogate_for_joint_distribution(
      dist=dist,
      base_distribution_surrogate_fn=base_distribution_surrogate_fn,
      num_auxiliary_variables=num_auxiliary_variables,
      global_auxiliary_variables=global_auxiliary_variables,
      variables=dist._model_unflatten(  # pylint: disable=protected-access
        _extract_variables_from_coroutine_model(
          posterior_generator, seed=seed)))

  # Temporary workaround for bijector caching issues with autobatched JDs.
  surrogate_type = joint_distribution_auto_batched.JointDistributionCoroutineAutoBatched
  if not hasattr(dist, 'use_vectorized_map'):
    surrogate_type = joint_distribution_coroutine.JointDistributionCoroutine
  surrogate_posterior = surrogate_type(posterior_generator,
                                       name=_get_name(dist))

  # Ensure that the surrogate posterior structure matches that of the prior.
  # todo: check me, do we need this? in case needs to be modified
  # if we use auxiliary variables, then the structure won't match the one of the
  # prior
  '''try:
    tf.nest.assert_same_structure(dist.dtype, surrogate_posterior.dtype)
  except TypeError:
    tokenize = lambda jd: jd._model_unflatten(
      # pylint: disable=protected-access, g-long-lambda
      range(len(jd._model_flatten(jd.dtype)))
      # pylint: disable=protected-access
    )
    surrogate_posterior = restructure.Restructure(
      output_structure=tokenize(dist),
      input_structure=tokenize(surrogate_posterior))(
      surrogate_posterior, name=_get_name(dist))'''
  return surrogate_posterior, variables


# todo: sample_shape and seed are not used.. maybe they should?
def _cf_convex_update_for_base_distribution(dist,
                                            initial_prior_weight,
                                            num_auxiliary_variables=0,
                                            global_auxiliary_variables=None,
                                            sample_shape=None,
                                            variables=None,
                                            seed=None):
  """Creates a trainable surrogate for a (non-meta, non-joint) distribution."""

  if variables is None:
    actual_event_shape = dist.event_shape_tensor()
    int_event_shape = int(actual_event_shape) if \
      actual_event_shape.shape.as_list()[0] > 0 else 1
    layers = 3
    bijectors = [reshape.Reshape([-1],
                                 event_shape_in=actual_event_shape +
                                                num_auxiliary_variables)]

    for _ in range(0, layers - 1):
      bijectors.append(
        build_highway_flow_layer(
          tf.reduce_prod(actual_event_shape + num_auxiliary_variables),
          residual_fraction_initial_value=initial_prior_weight,
          activation_fn=True, gate_first_n=int_event_shape, seed=seed))
    bijectors.append(
      build_highway_flow_layer(
        tf.reduce_prod(actual_event_shape + num_auxiliary_variables),
        residual_fraction_initial_value=initial_prior_weight,
        activation_fn=False, gate_first_n=int_event_shape, seed=seed))
    bijectors.append(
      reshape.Reshape(actual_event_shape + num_auxiliary_variables))

    variables = chain.Chain(bijectors=list(reversed(bijectors)))

  if num_auxiliary_variables > 0:
    batch_shape = global_auxiliary_variables.shape[0] if len(
      global_auxiliary_variables.shape) > 1 else []

    cascading_flows = split.Split(
      [-1, num_auxiliary_variables])(
      transformed_distribution.TransformedDistribution(
        distribution=blockwise.Blockwise([
          batch_broadcast.BatchBroadcast(dist,
                                         to_shape=batch_shape),
          independent.Independent(
            deterministic.Deterministic(
              global_auxiliary_variables),
            reinterpreted_batch_ndims=1)]),
        bijector=variables))

  else:
    cascading_flows = transformed_distribution.TransformedDistribution(
      distribution=dist,
      bijector=variables)

  return cascading_flows, variables


def _extract_variables_from_coroutine_model(model_fn, seed=None):
  """Extracts variables from a generator that yields (dist, variables) pairs."""
  gen = model_fn()
  try:
    dist, dist_variables = next(gen)
    flat_variables = [dist_variables]
    while True:
      seed, local_seed = samplers.split_seed(seed, n=2)
      sampled_value = (dist.distribution.sample(seed=local_seed)
                       if isinstance(dist, Root)
                       else dist.sample(seed=local_seed))
      dist, dist_variables = gen.send(
        sampled_value)  # tf.concat(sampled_value, axis=0)
      flat_variables.append(dist_variables)
  except StopIteration:
    pass
  return flat_variables


def _set_name(dist, name):
  """Copies a distribution-like object, replacing its name."""
  if hasattr(dist, 'copy'):
    return dist.copy(name=name)
  # Some distribution-like entities such as JointDistributionPinned don't
  # inherit from tfd.Distribution and don't define `self.copy`. We'll try to set
  # the name directly.
  dist = copy.copy(dist)
  dist._name = name  # pylint: disable=protected-access
  return dist


def _get_name(dist):
  """Attempts to get a distribution's short name, excluding the name scope."""
  return getattr(dist, 'parameters', {}).get('name', dist.name)
