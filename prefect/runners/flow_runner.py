import datetime
import prefect
from prefect.runners.task_runner import TaskRunner
from prefect.runners.state import TaskRunState, FlowRunState
from prefect.runners.results import RunResult
import uuid


def _deserialize_result(task, result):
    """
    Given a task and corresponding state, returns the state's
    deserialized result. If the state is anything but successful,
    None is returned.
    """
    if result is not None:
        try:
            return task.serializer.decode(result.result)
        except Exception as e:
            raise prefect.signals.FAIL(
                f'Could not deserialize result of {task}: {e}')


class FlowRunner:
    """
    The FlowRunner class takes a Flow and executes a FlowRun with a specific
    id.

    The FlowRunner is responsible for creating TaskRunners for each task in
    the flow and waits for them to complete. It returns the flow's state.
    """

    def __init__(self, flow, id=None, executor=None):

        self.flow = flow
        if id is None:
            id = uuid.uuid4().hex
        self.id = id

        if executor is None:
            executor = prefect.runners.executors.default_executor()
        self.executor = executor

    def run(self, state=None, context=None, **params):

        if state is None:
            state = prefect.runners.state.FlowRunState()

        for req in self.flow.required_params:
            if req not in params:
                raise ValueError(
                    'A required parameter was not provided: {}'.format(req))

        prefect_context = {
            'dt': None,  #TODO
            'as_of_dt': None,  # TODO
            'last_dt': None,  # TODO
            'flow': self.flow,
            'flow_id': self.flow.id,
            'flow_namespace': self.flow.namespace,
            'flow_name': self.flow.name,
            'flow_version': self.flow.version,
            'flowrun_id': self.id,
            'params': params,
        }
        if context is not None:
            prefect_context.update(context)

        # task_results contains a RunResult(state, result) for each task
        task_results = {}

        # deserialized holds futures representing deserialized task results
        # (to avoid deserializing the same result multiple times)
        deserialized = {}

        try:

            with self.executor(**prefect_context) as client:

                for task in self.flow.sorted_tasks():

                    upstream_results = {}
                    upstream_inputs = {}

                    # collect upstream results
                    for t in self.flow.upstream_tasks(task):
                        upstream_results[t.name] = task_results[t.name]

                    # check incoming edges to see if we need to deserialize
                    # any upstream inputs
                    for e in self.flow.edges_to(task):

                        # if the edge has no key, it's not an input
                        if e.key is None:
                            continue

                        u_task = e.upstream_task.name

                        # check if the task result is already deserialized
                        # (we only want to do this once)
                        if u_task not in deserialized:
                            deserialized[u_task] = client.submit(
                                _deserialize_result,
                                task=e.upstream_task,
                                result=task_results[u_task],
                                pure=False)

                        # get the (indexed) task result and store it under the
                        # appropriate input key
                        if e.upstream_index is not None:
                            upstream_inputs[e.key] = client.submit(
                                lambda result: result[e.upstream_index],
                                result=deserialized[u_task],
                                pure=False)
                        else:
                            upstream_inputs[e.key] = deserialized[u_task]

                    # run the task
                    # returns a RunResult(state, result)
                    task_results[task.name] = client.run_task(
                        task=task,
                        flowrun_id=self.id,
                        upstream_results=upstream_results,
                        inputs=upstream_inputs)

                # gather all task results from the cluster
                task_results = client.gather(task_results)

            terminal_results = {
                t.name: task_results[t.name]
                for t in self.flow.terminal_tasks()
            }

            if any(s.state.is_waiting() for s in task_results.values()):
                state.wait()
            elif any(s.state.is_failed() for s in terminal_results.values()):
                state.fail()
            elif all(s.state.is_successful() for s in terminal_results.values()):
                state.succeed()

        except Exception as e:
            state.fail(value='{}'.format(e))

        return RunResult(state=state, result=task_results)