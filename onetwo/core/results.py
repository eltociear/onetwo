# Copyright 2024 DeepMind Technologies Limited.
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

"""Data structures for storing results of prompting and experiment execution."""

from collections.abc import Callable, Mapping, Sequence
import copy
import dataclasses
import pprint
import textwrap
from typing import Any

import dataclasses_json
import termcolor


################################################################################
# Constants used as keys in the `inputs` and `outputs` mappings.
# Note that these are not intended to be an exhaustive list and are included
# here only for convenience, to avoid repeating the same raw string in multiple
# places in the code.
# TODO: Revisit how the results of individual calls to the
# language model should be represented, including multi-modal language models
# and `ScoreRequest` in addition to `CompleteRequest`.
# ################################################################################

# The request text to be sent to the engine.
INPUT_KEY_REQUEST = 'request'

# Reply text of the final reply in the exact form returned by the engine. Used
# in the result of sending a `BaseRequest` to a `LanguageModelEngine`.
OUTPUT_KEY_REPLY_TEXT = 'reply_text'
# Final reply text, with whitespace stripped. Used in the result of sending
# a `BaseRequest` to a `LanguageModelEngine`.
OUTPUT_KEY_REPLY_TEXT_STRIPPED = 'reply_text_stripped'
# Raw value returned by the underlying engine for the final reply, from which
# the reply text was extracted.
OUTPUT_KEY_REPLY_OBJECT = 'reply_object'
# Main output from a tool/callback/chain. This is used to indicate for example
# which output value to pass as inputs to further steps of computation.
MAIN_OUTPUT = 'output'
# Output field storing the repeated values to compute list metrics.
VALUES_FOR_LIST_METRICS = 'values_for_list_metrics'


def _exclude_empty(x: Any) -> bool:
  """Excludes empty values (when outputting dataclass_json.to_dict)."""
  return bool(not x)


@dataclasses_json.dataclass_json
@dataclasses.dataclass
class ExecutionResult:
  """Full results of prompt or chain execution, including debug details.

  Note that this is a nested data structure and is designed such that the same
  data structure can be used to represent the results of executing a prompting
  strategy as a whole (e.g., for one given example from a dataset), or the
  result of sending a single request to an underlying language model, or
  anything in between.

  Attributes:
    stage_name: Name of the corresponding prompt stage, in the case where the
      execution results are for one of the stages of an outer prompt chain.
    inputs: The inputs to the current prompting stage -- e.g., the contents of
      the parsed dataset record (in the case of a top-level ExecutionResult) or
      the output variables of a preceding chain (in the case of an intermediate
      result). For a leaf-level ExecutionResult corresponding to a single
      `CompleteRequest` sent to a text-only language model, this would contain a
      key called 'request', along with any input variables that were used in
      constructing that request.
    outputs: The outputs of the current prompting stage. For a leaf-level
      ExecutionResult corresponding to a single `CompleteRequest` sent to a
      text-only language model, this would contain a few hard-coded keys called
      'reply_object', 'reply_text', and 'reply_text_stripped', along with a
      value for the engine reply placeholder. For a multi-step prompt template
      bundled with reply parsing logic, this would contain the result of parsing
      each of the engine replies received in the course of the prompt template
      execution, with the result of later replies overwriting those of previous
      replies, in the case of name clashes. For a prompt chain, the structure of
      the outputs would be determined by the chain implementation.
    stages: In the case of a prompt chain (or of a multi-step prompt template),
      this contains the execution results of each of the steps in the chain, in
      order of execution.
    error: Error message in the case where an error occurred.
    info: Contains identifying information for the given data point -- e.g.,
      record_id, exemplar_list_id, exemplar_list_size, sample_id, sample_size.
      For now, this is populated only on top-level ExecutionResults.
  """
  # Disabling type checking due to a wrong type annotation in dataclasses_json:
  # https://github.com/lidatong/dataclasses-json/issues/336
  # pytype: disable=wrong-arg-types

  # Fields relevant only for the sub-stages of a PromptChain.
  stage_name: str = dataclasses.field(
      default='', metadata=dataclasses_json.config(exclude=_exclude_empty)
  )

  # Fields relevant at arbitrary levels of nesting.
  inputs: dict[str, Any] = dataclasses.field(default_factory=dict)
  outputs: dict[str, Any] = dataclasses.field(default_factory=dict)
  stages: list['ExecutionResult'] = dataclasses.field(
      default_factory=list,
      metadata=dataclasses_json.config(exclude=_exclude_empty),
  )
  error: str = dataclasses.field(
      default='', metadata=dataclasses_json.config(exclude=_exclude_empty)
  )

  # Fields currently relevant only at the top level.
  # TODO: We should revisit whether it makes sense to use
  # an info data structure like this in lower levels of the execution hierarchy
  # as well; if not, it may be cleaner to move this into `ExperimentResult`.
  info: dict[str, Any] = dataclasses.field(
      default_factory=dict,
      metadata=dataclasses_json.config(exclude=_exclude_empty),
  )

  # pytype: enable=wrong-arg-types

  def get_leaf_results(self) -> list['ExecutionResult']:
    """Returns references to the leaves of the result hierarchy."""
    leaf_results = []
    if self.stages:
      for stage in self.stages:
        leaf_results.extend(stage.get_leaf_results())
    else:
      leaf_results.append(self)
    return leaf_results

  def format(self, color: bool = True) -> str:
    """Returns a pretty-formatted version of the result hierarchy.

    Args:
      color: If True, then will return a string that is annotated with
        `termcolor` to apply colors and boldfacing when the text is printed to
        the terminal or in a colab. If False, then will return just plain text.
    """
    lines = []
    if self.stages:
      # Non-leaf result.
      for stage in self.stages:
        if stage.stage_name:
          if color:
            lines.append(
                termcolor.colored(stage.stage_name, attrs=['bold', 'underline'])
            )
          else:
            lines.append(stage.stage_name)
          lines.append(textwrap.indent(stage.format(color=color), '  '))
        else:
          lines.append(stage.format(color=color))
      for key, value in self.outputs.items():
        if color:
          lines.append(termcolor.colored(f'Parsed {key}:', attrs=['bold']))
          lines.append(termcolor.colored(str(value), 'magenta'))
        else:
          lines.append(f'* Parsed {key}')
          lines.append(str(value))
    else:
      # Leaf result.
      show_reply_on_new_line = True
      request = str(self.inputs.get(INPUT_KEY_REQUEST, None))
      if OUTPUT_KEY_REPLY_TEXT in self.outputs:
        # Show text replies from LLMs as a continuation of the request prompt.
        reply = str(self.outputs.get(OUTPUT_KEY_REPLY_TEXT, None))
        show_reply_on_new_line = False
      elif 'reply' in self.outputs:
        # For a BaseReply received from a tool, show the reply object.
        reply = str(self.outputs.get('reply', None))
      else:
        # Otherwise, fall back to showing the whole outputs data structure.
        reply = str(self.outputs)
      if color:
        lines.append(termcolor.colored('Request/Reply', attrs=['bold']))
        formatted_reply = termcolor.colored(reply, 'blue')
      else:
        lines.append('* Request/Reply')
        formatted_reply = f'<<{reply}>>'
      if show_reply_on_new_line:
        lines.append(f'{request}\n{formatted_reply}')
      else:
        lines.append(f'{request}{formatted_reply}')

    formatted_text = '\n'.join(lines)
    return formatted_text

  def get_reply_summary(self) -> str:
    """Returns a summary of replies useful for logging."""
    # We abbreviate nested input fields like 'exemplar' and nested output
    # fields like 'reply_object', as these are too bulky to be readable.
    def _pformat_abbreviated(original: dict[Any, Any]) -> str:
      abbreviated = original.copy()
      for k, v in abbreviated.items():
        if isinstance(v, dict) or isinstance(v, list):
          abbreviated[k] = '...'
      return pprint.pformat(abbreviated)

    result = (
        '\n\n=======================================================\n'
        f'Inputs: {_pformat_abbreviated(self.inputs)}\n'
        '--------------\n'
        f'Outputs: {_pformat_abbreviated(self.outputs)}'
    )
    if self.error:
      result += f'--------------\nError: {self.error}\n'
    return result


def format_result(
    result: ExecutionResult | Sequence[ExecutionResult],
    color: bool = True,
) -> str:
  """Returns a pretty-formatted version of the result hierarchy.

  See `ExecutionResult.format` for details. This function does this same
  thing, but also support formatting of a sequence of results.

  Args:
    result: The result or sequence of results to format.
    color: See `ExecutionResult.format`.
  """
  if isinstance(result, Sequence):
    texts = []
    for i, res in enumerate(result):
      if color:
        preamble = termcolor.colored(
            f'Sample {i + 1}/{len(result)}\n',
            'green',
            attrs=['bold', 'underline'],
        )
      else:
        preamble = f'* Sample {i + 1}/{len(result)}\n'
      texts.append(preamble + res.format(color=color))
    return '\n\n'.join(texts)
  else:
    return result.format(color=color)


def apply_formatting(
    result: ExecutionResult,
    function: Callable[[ExecutionResult], str]
) -> str:
  """Formats the ExecutionResult by applying a function at each node.

  Args:
    result: ExecutionResult to be formatted.
    function: A function taking an ExecutionResult and returning a string
      representation.

  Returns:
    A tree representation of the ExecutionResult where each node is represented
    using the function.
  """
  res = function(result)
  subres = ''
  for stage in result.stages:
    subres += apply_formatting(stage, function)
  return res + textwrap.indent(subres, '  ')


def get_name_tree(result: ExecutionResult) -> str:
  """Returns a tree representation with the stage names for easy inspection."""
  return apply_formatting(
      result, lambda s: f'- {s.stage_name}\n' if s.stage_name else '-\n'
  )


def _trim_key(key: str) -> str:
  if len(key) < 30:
    return key
  else:
    return key[:27] + '...'


def _trim_value(value: str) -> str:
  if len(value) < 50:
    return value
  else:
    return value[:23] + '[...]' + value[-23:]


def get_name_keys_tree(result: ExecutionResult) -> str:
  """Returns a tree with the stage names and input/output keys."""

  def formatting(result: ExecutionResult) -> str:
    inputs = list(map(_trim_key, result.inputs.keys()))
    outputs = list(map(_trim_key, result.outputs.keys()))
    return f'- {result.stage_name}: {inputs} -> {outputs}\n'

  return apply_formatting(result, formatting)


def get_short_values_tree(result: ExecutionResult) -> str:
  """Returns a tree with the values trimmed to a single line."""

  def render_dict(d: Mapping[str, Any]) -> str:
    trimmed = {_trim_key(k): _trim_value(repr(v)) for k, v in d.items()}
    if len(d.keys()) <= 1:
      return str(d)
    return (
        '{\n'
        + '\n'.join([f'    {k}: {v}' for k, v in trimmed.items()])
        + '\n  }'
    )

  def formatting(result: ExecutionResult) -> str:
    return (
        f'- {result.stage_name}:\n  inputs: {render_dict(result.inputs)}\n '
        f' outputs: {render_dict(result.outputs)}\n'
    )

  return apply_formatting(result, formatting)


@dataclasses_json.dataclass_json
@dataclasses.dataclass
class ExperimentResult(ExecutionResult):
  """Full results of an experiment run on a given example, with metrics.

  Corresponds more or less one-to-one to the contents of a single record of the
  'results_debug.json' file that is output at the end of each experiment run.
  The 'results.json' file  contains the same content, but with the 'stages'
  field and a few keys of the other mappings omitted.

  Attributes:
    targets: Target values (i.e., "golden" outputs), against which to compare
      the outputs when calculating metrics.
    metrics: Evaluation metrics, such as accuracy.
  """
  # Disabling type checking due to a wrong type annotation in dataclasses_json:
  # https://github.com/lidatong/dataclasses-json/issues/336
  # pytype: disable=wrong-arg-types

  targets: dict[str, Any] = dataclasses.field(
      default_factory=dict,
      metadata=dataclasses_json.config(exclude=_exclude_empty),
  )
  metrics: dict[str, Any] = dataclasses.field(
      default_factory=dict,
      metadata=dataclasses_json.config(exclude=_exclude_empty),
  )
  # pytype: enable=wrong-arg-types

  def to_compact_record(self) -> 'ExperimentResult':
    """Returns a compact version of self for writing to results.json."""
    record_compact = copy.deepcopy(self)
    record_compact.stages = []

    # We enumerate here any keys that we prefer to omit from the compact
    # representation of the input and output formats, due to the content being
    # too bulky (as in the case of `exemplar` or `VALUES_FOR_LIST_METRICS`) or
    # repetitive content that is included in the intenral results data structure
    # only for legacy reasons (as in the case of `original` and `record_id`).
    input_fields_to_omit = {'exemplar', 'original', 'record_id'}
    output_fields_to_omit = {VALUES_FOR_LIST_METRICS}

    for key in input_fields_to_omit:
      if key in record_compact.inputs:
        del record_compact.inputs[key]
    for key in output_fields_to_omit:
      if key in record_compact.outputs:
        del record_compact.outputs[key]
    return record_compact

  @classmethod
  def from_execution_result(
      cls, execution_result: ExecutionResult
  ) -> 'ExperimentResult':
    """Returns an ExperimentResult with the same content as execution_result."""
    experiment_result = ExperimentResult()
    for field in dataclasses.fields(ExecutionResult):
      setattr(
          experiment_result, field.name, getattr(execution_result, field.name)
      )
    return experiment_result


def execution_result_from_dict(data: dict[str, Any]) -> ExecutionResult:
  """Returns an ExecutionResult restored from a structure created by to_dict."""
  result = ExecutionResult.from_dict(data)
  # Theoretically `ExecutionResult.from_dict` alone should be sufficient, but
  # for some reason when we try to do this, the nested stage results fail to get
  # converted from dicts back into ExecutionResult objects. Might be a
  # limitation of dataclasses_json with respect to self-referential data
  # structures.
  result.stages = list(execution_result_from_dict(s) for s in result.stages)
  return result


def experiment_result_from_dict(data: dict[str, Any]) -> ExperimentResult:
  """Returns an ExperimentResult restored from structure created by to_dict."""
  result = ExperimentResult.from_dict(data)
  # See note on `execution_result_from_dict` above for why this is needed.
  result.stages = list(execution_result_from_dict(s) for s in result.stages)
  return result
