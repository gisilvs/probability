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
"""Numpy stub for `tensor_spec`."""

__all__ = [
    'TensorSpec',
]


class DenseSpec(object):

  def __init__(self, shape, dtype, name=None):
    self.shape = shape
    self.dtype = dtype
    self.name = name

  def __repr__(self):
    return '{}(shape={}, dtype={}, name={})'.format(
        type(self).__name__, self.shape, repr(self.dtype), repr(self.name))


class TensorSpec(DenseSpec):
  pass
