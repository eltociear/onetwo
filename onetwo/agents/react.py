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

"""Implementation of a ReAct strategy using the Agent framework.

Generalized from the ideas of the ReAct paper: https://arxiv.org/abs/2210.03629

Adopting the "Agent" framework for the ReAct implementation means that:
* The full state of the ReAct strategy at each step is encapsulated in a
  serializable state object (ReActState), which in this case is represented as a
  series of steps, each of which consists of some combination of thought, action
  (i.e., FunctionCall), and observation.
* The prompt template that defines the calls made to the LLM in each step takes
  such a state object as input and performs exactly one step of the strategy.
  The step-loop is implemented in the agent code, rather than in the prompt
  template.
* This ensures that in addition to running the full ReAct strategy end-to-end,
  we are also able to support stop/restart, stepwise execution, and composition
  of ReAct with other agent strategies such as tree-of-thought, for pursuing
  multiple possible trajectories in parallel.
"""

import abc
from collections.abc import AsyncIterator, Sequence
import contextlib
import dataclasses
import re
from typing import Any, Protocol

from onetwo.agents import base as agents_base
from onetwo.builtins import prompt_templating
from onetwo.core import constants
from onetwo.core import executing
from onetwo.core import templating
from onetwo.core import tracing
from onetwo.stdlib.tool_use import llm_tool_use
from onetwo.stdlib.tool_use import python_tool_use


@dataclasses.dataclass
class ReActStep:
  """One step of a serializable ReAct agent state.

  Attributes:
    is_finished: Whether this is intended to be the final state. If True, then
      the observation can be treated as the final answer.
    thought: Thought that was output by the LLM (if any). In the case where a
      single thought is followed by several actions, this will be represented by
      a series of steps (one for each action), with the thought populated on
      just the first step.
    action: Action that was executed in this step (if any). In the case where
      multiple thoughts are output before executing an action, the initial
      thoughts will be represented as their own steps, with the action left as
      None.
    observation: Observation that resulted from execution of the action; or in
      the special case of a force-finish (where we explicit prompt the LLM with
      '[Finish]'), this represents the final response from the LLM. In the case
      where `is_finished` is True, this observation can be treated as the final
      answer.
    fmt: Format in which the action (and observation) are to be rendered when
      representing in text form for display to the LLM. In the case where the
      action was generated by the LLM itself, this is the action format that was
      detected when parsing the LLM reply.
  """

  is_finished: bool = False
  thought: str = ''
  action: llm_tool_use.FunctionCall | None = None
  observation: Any = None
  fmt: llm_tool_use.ArgumentFormat | None = None

  def render_action(self) -> str:
    """Returns the action formatted appropriately for insertion in a prompt."""
    if self.action is not None:
      return self.action.render(fmt=self.fmt)
    else:
      return str(self.action)

  def render_observation(self) -> str:
    """Returns the observation formatted for insertion in a prompt."""
    return llm_tool_use.render_response(fmt=self.fmt, value=self.observation)


# In the ReAct strategy, the state consists of a monotonically increasing
# sequence of steps, each of which may involve a thought and/or action.
ReActState = agents_base.UpdateListState[str, ReActStep]

DEFAULT_REACT_PROMPT_TEXT = """\
{#- Preamble: Tools description -#}
{%- role name='system' -%}
Here is a list of available tools:
{% for tool in tools %}
Tool name: {{ tool.name }}
Tool description: {{ tool.description }}
{% if tool.example -%}
  Tool example: {{ tool.example_str }}
{%- endif -%}
{% endfor %}

{#- Preamble: ReAct few-shots #}
Here are examples of how different tasks can be solved with these tools:
{% for example in exemplars %}
[{{ stop_prefix }}Question]: {{ example.inputs + '\n' }}
{%- for step in example.updates -%}
{%- if step.thought -%}
  [Thought]: {{ step.thought + '\n' }}
{%- endif -%}
{%- if step.action -%}
  [Act]: {{ step.render_action() + '\n' }}
{%- endif -%}
{%- if step.observation and step.action -%}
  [{{ stop_prefix }}Observe]: {{ step.render_observation() + '\n' }}
{%- endif -%}
{%- if step.is_finished and step.observation and not step.action -%}
  [Finish]: {{ step.observation + '\n' }}
{%- endif -%}
{%- endfor -%}
{%- endfor -%}

{# Start of the processing of the actual inputs. -#}

{#- Render the original question. -#}
{%- endrole -%}
{%- role name='user' %}
[{%- role name='system' -%}{{ stop_prefix }}{%- endrole -%}Question]: {{ state.inputs + '\n' }}
{%- endrole -%}

{# Render the current state (i.e., any steps performed up till now). -#}
{%- for step in state.updates -%}
{%- if step.thought -%}
  [Thought]: {{ step.thought + '\n' }}
{%- endif -%}
{%- if step.action -%}
  [Act]: {{ step.render_action() + '\n' }}
{%- endif -%}
{%- if step.observation and step.action -%}
  [{{ stop_prefix }}Observe]: {{ step.render_observation() + '\n' }}
{%- endif -%}
{%- if step.is_finished and step.observation and not step.action -%}
  [Finish]: {{ step.observation + '\n' }}
{%- endif -%}
{%- endfor -%}

{# If force-finishing, then prompt the LLM for the final answer. -#}
{%- if force_finish -%}
  [Finish]:{{ ' ' }}
{%- endif -%}

{#- Get a response from the LLM and return it. -#}
{%- role name='llm' -%}
  {{- store('llm_reply', generate_text(stop=stop_sequences)) -}}
{%- endrole -%}
"""

# Default set of exemplars that can be used as the `exemplars` for calls
# to a ReAct prompt.
REACT_FEWSHOTS = [
    ReActState(
        inputs='How much taller is Everest than K2?',
        updates=[
            ReActStep(
                thought=(
                    'First we need to find out how tall are Everest and K2. We'
                    ' can use the Search tool for that.'
                ),
                action=llm_tool_use.FunctionCall(
                    function_name='Search',
                    args=('how tall is Everest?',),
                    kwargs={},
                ),
                observation='8,849 m',
                fmt=llm_tool_use.ArgumentFormat.PYTHON,
            ),
            ReActStep(
                action=llm_tool_use.FunctionCall(
                    function_name='Search', args=('how tall is K2?',), kwargs={}
                ),
                observation='8,611 m',
                fmt=llm_tool_use.ArgumentFormat.PYTHON,
            ),
            ReActStep(
                thought=(
                    'Now we need to subtract their heights. We can use the'
                    ' Python tool for that.'
                ),
                action=llm_tool_use.FunctionCall(
                    function_name='Python', args=('8849 - 8611',), kwargs={}
                ),
                observation='238',
                fmt=llm_tool_use.ArgumentFormat.PYTHON,
            ),
            ReActStep(
                is_finished=True,
                thought='Everest is 238 meters taller than K2.',
                action=llm_tool_use.FunctionCall(
                    function_name='Finish', args=('238 meters',), kwargs={}
                ),
                observation='238 meters',
                fmt=llm_tool_use.ArgumentFormat.PYTHON,
            ),
        ],
    ),
    ReActState(
        inputs=(
            'Spell the name of the scientist who invented relativity backwards.'
        ),
        updates=[
            ReActStep(
                thought=(
                    'First we need to find out who invented relativity. We can'
                    ' use the Search tool for that.'
                ),
                action=llm_tool_use.FunctionCall(
                    function_name='Search',
                    args=('who invented relativity?',),
                    kwargs={},
                ),
                observation='Albert Einstein',
                fmt=llm_tool_use.ArgumentFormat.PYTHON,
            ),
            ReActStep(
                thought=(
                    'Now we can use the Python tool to spell it backwards. We'
                    ' need to write a function that inverts the letters of its'
                    ' input and then apply it to the name retrieved above:'
                ),
                action=llm_tool_use.FunctionCall(
                    function_name='Python',
                    args=(),
                    kwargs={
                        'request': (
                            'def invert_letters(input_str):\n  return'
                            ' input_str[::-1]\nresult = invert_letters("Albert'
                            ' Einstein")'
                        )
                    },
                ),
                observation='nietsniE treblA',
                fmt=llm_tool_use.ArgumentFormat.YAML_CODE,
            ),
            ReActStep(
                is_finished=True,
                thought=(
                    'Albert Einstein invented relativity and his name backwards'
                    ' is nietsniE treblA.'
                ),
                action=llm_tool_use.FunctionCall(
                    function_name='Finish', args=('nietsniE treblA',), kwargs={}
                ),
                observation='nietsniE treblA',
                fmt=llm_tool_use.ArgumentFormat.PYTHON,
            ),
        ],
    ),
]


class ReActPromptProtocol(Protocol):
  """Interface for prompt usable with ReActAgent.prompt."""

  @executing.make_executable
  @abc.abstractmethod
  async def __call__(
      self,
      force_finish: bool,
      exemplars: list[ReActState],
      state: ReActState,
      stop_prefix: str,
      stop_sequences: list[str],
      tools: Sequence[llm_tool_use.Tool],
  ) -> str:
    """Executes the prompt template on the given args and returns the result.

    Args:
      force_finish: Whether to prompt the LLM to output a final answer now
        rather than continuing with more steps.
      exemplars: Few-shot exemplars to include in the prompt.
      state: Current state of the ReAct strategy, i.e., the original inputs and
        sequence of steps (if any) that have been performed so far.
      stop_prefix: The string that is used to mark positions for early stopping.
        This is used for the [Question] and [Observe] stages. By default, no
        stop prefix is used.
      stop_sequences: The stop sequences to specify to the LLM (i.e., the
        substrings at which the LLM will truncate the reply. The stop sequences
        are not included in the truncated reply.
      tools: The tools that are available to be used and whose descriptions are
        to be listed in the prompt. Any tools referenced in the `exemplars`
        should be registered here, although it is not strictly required for all
        of the tools to be illustrated in `exemplars`.

    Returns:
      The LLM reply. The caller is responsible for parsing this into thought,
      action, etc., and for executing the action where relevant.
    """


@dataclasses.dataclass
class ReActPromptJ2(
    ReActPromptProtocol, prompt_templating.JinjaTemplateWithCallbacks
):
  """JinjaTemplate usable with ReActAgent.prompt."""

  # Overriding default value of attribute defined in templating.JinjaTemplate.
  text: str = DEFAULT_REACT_PROMPT_TEXT

  @executing.make_executable
  async def __call__(
      self,
      force_finish: bool,
      exemplars: list[ReActState],
      state: ReActState,
      stop_prefix: str,
      stop_sequences: list[str],
      tools: Sequence[llm_tool_use.Tool],
  ) -> str:
    """See ReActPromptProtocol."""
    result = await self.render(
        force_finish=force_finish,
        exemplars=exemplars,
        state=state,
        stop_prefix=stop_prefix,
        stop_sequences=stop_sequences,
        tools=tools,
    )
    return result['llm_reply']


class ReActParseProtocol(Protocol):
  """Interface for parsing the LLM reply for a ReAct prompt."""

  @abc.abstractmethod
  def __call__(
      self,
      reply_text: str,
  ) -> ReActStep:
    """Returns the result of parsing the LLM reply for a ReAct prompt.

    Args:
      reply_text: String containing LLM's completion.

    Returns:
      ReActStep containing all of the information that can be determined from
      the LLM reply alone (without actually executing the action). In the case
      where the LLM reply contained an action, this means that `observation`
      will be left as `None`. In the case where the LLM chose to "Finish",
      however, the returned ReActStep will be complete, with `observation`
      containing the final answer.
    """


def react_parse(
    reply_text: str,
    *,
    action_pattern: str = r'\[Act\]:',
    thought_pattern: str = r'\[Thought\]:',
    finish_pattern: str = r'\[Finish\]:',
    final_stop_sequence: str | None = '\n\n',
) -> ReActStep:
  """Returns the result of parsing the LLM reply for a ReAct prompt.

  Args:
    reply_text: String containing LLM's completion.
    action_pattern: Regex pattern indicating the beginning of an action.
    thought_pattern: Regex pattern indicating the beginning of a thought.
    finish_pattern: Regex pattern indicating the beginning of a finish line.
    final_stop_sequence: Additional stop sequence at which to truncate the final
      answer after retrieving from the LLM.

  Returns:
    ReActStep containing all of the information that can be determined from the
    LLM reply alone (without actually executing the action). In the case where
    the LLM reply contained an action, this means that `observation` will be
    left as `None`. In the case where the LLM chose to "Finish", however, the
    returned ReActStep will be complete, with `observation` containing the final
    answer.
  """
  # Creating an empty prompt text in imitation of `react_chain_j2_test.py`.
  # TODO: Would it ever make sense for the prompt template context
  # to be non-empty when parsing a ReAct reply? E.g., would it ever make sense
  # for the context to contain some predefined variables that could be
  # referenced as function arguments in the action string?
  prompt_context = templating.PromptTemplateContext()

  try:
    # Find '[Act]:' (or variant thereof, e.g. 'Action 1:', etc.).
    match_act = re.search(action_pattern, reply_text)
    if match_act:
      act_start, act_end = match_act.span()
    else:
      act_start = act_end = len(reply_text)

    # Find '[Thought]:' (or variant thereof, e.g. 'Thought 1:', etc.).
    match_thought = re.search(thought_pattern, reply_text)
    if match_thought:
      thought_start, thought_end = match_thought.span()
    else:
      thought_start = thought_end = len(reply_text)

    # Find '[Finish]:' (or variant thereof).
    match_finish = re.search(finish_pattern, reply_text)
    if match_finish:
      finish_start, finish_end = match_finish.span()
    else:
      finish_start = finish_end = len(reply_text)

    thought_content = ''
    if act_start < finish_start:
      # Act is first.
      if thought_start < act_start:
        thought_content = reply_text[thought_end:act_start].strip()
      part_after_act = reply_text[act_end:].strip()

      _, fn, args, kwargs, fmt, _ = llm_tool_use.parse_and_consume_call(
          text=part_after_act, context_vars=prompt_context.context_variables
      )
      is_finished = fn == 'Finish'
      action = llm_tool_use.FunctionCall(
          function_name=fn, args=args, kwargs=kwargs
      )
      return ReActStep(
          is_finished=is_finished,
          thought=thought_content,
          action=action,
          fmt=fmt,
      )
    elif finish_start < act_start:
      # Finish is first.
      if thought_start < finish_start:
        thought_content = reply_text[thought_end:finish_start].strip()
      final_answer = reply_text[finish_end:].strip()
      if final_stop_sequence:
        final_answer = final_answer.split(final_stop_sequence)[0].strip()
      return ReActStep(
          is_finished=True, thought=thought_content, observation=final_answer
      )
    else:
      # None found.
      raise ValueError(f"Didn't find {action_pattern} or {finish_pattern}")
  except ValueError as e:
    # We catch all ValueErrors and echo them back to the LLM as an observation.
    # TODO: Verify that the error gets echoed back properly.
    return ReActStep(
        is_finished=False, observation=f'{constants.ERROR_STRING}: {e}'
    )


@dataclasses.dataclass
class ReActAgent(
    agents_base.SingleSampleAgent[
        str,  # _I (inputs)
        str,  # _O (outputs)
        ReActState,  # _S (state)
        ReActStep,  # _U (update)
        python_tool_use.PythonToolUseEnvironment,  # _E (environment)
    ]
):
  """Agent for the ReAct strategy, doing sequence of thought/action/observation.

  Attributes:
    prompt: Prompt template used for prompting the LLM at each step.
    parse: Function for parsing an LLM reply into a `ReActStep`.
    exemplars: Few-shot exemplars to include in the prompt.
    environment_config: Config controlling the behavior of environments created
      by this agent. This includes list of the tools that are available to be
      used and whose descriptions are to be listed in the prompt. Any tools
      referenced in the `exemplars` should be registered here, although it is
      not strictly required for all of the tools to be illustrated in
      `exemplars`.
    max_steps: Number of ReAct iterations after which the agent will be forced
      to finish.
    stop_prefix: The string that is used to mark positions for early stopping.
      This is used for the [Question] and [Observe] stages. By default, no stop
      prefix is used.
  """

  prompt: ReActPromptProtocol = dataclasses.field(default_factory=ReActPromptJ2)
  parse: ReActParseProtocol = dataclasses.field(default=react_parse)
  exemplars: list[ReActState] = dataclasses.field(default_factory=list)
  # TODO: Decouple the choice of environment from the agent.
  environment_config: python_tool_use.PythonToolUseEnvironmentConfig = (
      dataclasses.field(
          default_factory=python_tool_use.PythonToolUseEnvironmentConfig
      )
  )
  max_steps: int = 10
  stop_prefix: str = ''

  def _get_stop_sequences(self) -> list[str]:
    """Returns the list of stop sequences to use for the prompt."""
    return (
        [f'[{self.stop_prefix}']
        if self.stop_prefix
        else ['[Question]', '[Observe]']
    )

  @executing.make_executable(copy_self=False)
  async def initialize_state(self, inputs: str) -> ReActState:
    """Returns a newly initialized state based on the input question.

    Overridden from base class (Agent).

    Args:
      inputs: Input to the agent, representing the overall goal that the agent
        is trying to achieve.
    """
    return ReActState(inputs=inputs)

  @contextlib.asynccontextmanager
  async def start_environment(
      self,
  ) -> AsyncIterator[python_tool_use.PythonToolUseEnvironment]:
    """Context manager to start the environment.

    Usage:
    ```
      agent = ...
      async with agent.start_environment() as env:
         # In here, we can call other methods on `agent` using `env` as the
         # environment.
    ```

    Yields:
      Environment object, which will be automatically cleaned up when exiting
      the `with` block.
    """
    with python_tool_use.PythonToolUseEnvironment(
        config=self.environment_config
    ) as env:
      yield env

  @executing.make_executable(copy_self=False, non_copied_args=['environment'])
  @tracing.trace('ReActAgent._sample_single_next_step')
  async def _sample_single_next_step(
      self,
      state: ReActState,
      environment: python_tool_use.PythonToolUseEnvironment,
  ) -> ReActStep:
    """Runs one step of the strategy and returns a new resulting state.

    Overridden from base class (SingleSampleAgent).

    Args:
      state: Current state of the agent.
      environment: Environment in which to perform the operation. Not relevant
        for ReAct currently. (In the future, this could store the ToolHandler.)

    Returns:
      An incremental update to the agent state that would occur as a result of
      performing the given step.
    """
    if environment is None:
      raise ValueError('Environment must be specified for this agent.')

    # Prompt the LLM to determine the next action to take.
    force_finish = len(state.updates) >= self.max_steps
    llm_reply = await self.prompt(
        tools=environment.config.tools,
        exemplars=self.exemplars,
        stop_prefix=self.stop_prefix,
        stop_sequences=self._get_stop_sequences(),
        state=state,
        force_finish=force_finish,
    )

    # Parse the LLM response and execute the selected action.
    llm_reply = llm_reply.strip() + '\n'
    if force_finish:
      return ReActStep(
          is_finished=True,
          thought='',
          action=None,
          observation=llm_reply.strip(),
      )
    else:
      next_step = self.parse(llm_reply)
      # TODO: Support variable reference and assignment in the
      # `llm_reply`.
      if next_step.action:
        # Note that if we assume that the environment is always registered at
        # the time we reach here, calling `environment.run_tool` like below is
        # equivalent to calling the builtin `tool_use.run_tool`.
        next_step.observation = await environment.run_tool(
            tool_name=next_step.action.function_name,
            tool_args=next_step.action.args,
            tool_kwargs=next_step.action.kwargs,
        )
      return next_step

  def is_finished(self, state: ReActState) -> bool:
    """Returns whether the strategy is in finished state.

    Overridden from base class (Agent).

    Args:
      state: Current state of the agent.
    """
    return bool(state.updates and state.updates[-1].is_finished)

  def extract_output(self, state: ReActState) -> str:
    """Returns the final output from the strategy, based on the state.

    Overridden from base class (Agent).

    Args:
      state: Current (presumably final) state of the agent.
    """
    # TODO: Consider how to support non-string outputs.
    if state.updates and state.updates[-1].is_finished:
      answer = state.updates[-1].observation
    else:
      answer = None
    return str(answer)
