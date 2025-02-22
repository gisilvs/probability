# Copyright 2020 The TensorFlow Probability Authors.
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
# [internal] load pytype.bzl (pytype_strict_library)
# [internal] load strict.bzl

licenses(["notice"])

package(default_visibility = ["//visibility:public"])

# pytype_strict
py_library(
    name = "state",
    srcs = ["__init__.py"],
    deps = [
        ":api",
        ":function",
        ":module",
        ":registrations",
    ],
)

# pytype_strict
py_library(
    name = "api",
    srcs = ["api.py"],
    deps = [
        ":module",
        # jax dep,
    ],
)

# pytype_strict
py_library(
    name = "function",
    srcs = ["function.py"],
    deps = [
        ":api",
        ":module",
        # jax dep,
        "//oryx/core:kwargs_util",
        "//oryx/core:trace_util",
        "//oryx/core/interpreters:harvest",
        "//oryx/core/interpreters/inverse:custom_inverse",
    ],
)

# pytype_strict
py_library(
    name = "module",
    srcs = ["module.py"],
    deps = [
        # jax dep,
        "//oryx/core:primitive",
        "//oryx/core:pytree",
        "//oryx/core/interpreters:harvest",
        "//oryx/core/interpreters:log_prob",
        "//oryx/core/ppl",
    ],
)

# pytype_strict
py_library(
    name = "registrations",
    srcs = ["registrations.py"],
    deps = [
        ":api",
        # jax dep,
    ],
)

# py_strict
py_test(
    name = "function_test",
    srcs = ["function_test.py"],
    deps = [
        ":api",
        ":function",
        ":module",
        # absl/testing:absltest dep,
        # jax dep,
        # numpy dep,
        "//oryx/core:primitive",
        "//oryx/internal:test_util",
    ],
)

# py_strict
py_test(
    name = "registrations_test",
    srcs = ["registrations_test.py"],
    deps = [
        ":api",
        ":function",
        ":registrations",
        # absl/testing:absltest dep,
        # jax dep,
        "//oryx/internal:test_util",
    ],
)
