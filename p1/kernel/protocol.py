"""Protocol: 24 methods. Requests are sent by clients via POST /rpc (JSON-RPC 2.0);
notifications are pushed by the server via GET /events (SSE).

Read-only queries (snapshots, nodes/files by hash, evidence details, LLM call logs, edit previews)
are resource reads outside the RPC protocol. Budget reservations and settlements live in SQLite
without emitting events; a warning accompanies goal/stateChanged when usage reaches 80%.
"""

REQUESTS = {
    'session/start': {'required': [], 'optional': ['goal', 'template', 'approval_mode', 'run_preset', 'text', 'mode',
                                                   'llm', 'docs', 'talk'],
                      'doc': ('Open an existing goal or create a new goal draft from a text prompt or template; '
                              'mode is chat or work, llm is {group, model, effort}, docs is a list of uploaded '
                              'document hashes. The kernel runs prechecks immediately and extracts parameters when docs are provided')},
    'session/resume': {'required': ['session'], 'optional': ['after_seq'],
                       'doc': 'Return the current snapshot and events after after_seq'},
    'session/interrupt': {'required': ['goal'], 'optional': [],
                          'doc': 'Immediate stop: terminate running solver processes and confirm stop (stopped or stop_unconfirmed)'},
    'goal/confirm': {'required': ['goal', 'version'], 'optional': ['approval_mode'],
                     'doc': 'Confirm the goal case list; returns 409 with current version on mismatch'},
    'goal/estimate': {'required': ['presets'], 'optional': ['goal'],
                      'doc': ('Estimate OpenMC runtime for each particle preset (named tier or "<particles>x<batches>") '
                              'without mutating state. When goal is given, estimates remaining cases not already cached '
                              'under that preset; otherwise estimates a single case. Uses live coordinator node queues '
                              'or falls back to local serial timing when offline or dispatch is disabled')},
    'task/submit': {'required': ['goal', 'kind'], 'optional': ['input'],
                    'doc': ('Submit text input (handled by LLM returning review/trial/work proposals), edit params, '
                            'switch reading, change binding, rerun or change preset, generate report, archive, toggle '
                            'chat/work mode, change approval policy, start work (checking optional input.version with 409 '
                            'on mismatch), add/delete user rules, batch-save rule edits, or pick LLM model and effort')},
    'approval/respond': {'required': ['approval', 'decision'], 'optional': ['text'],
                         'doc': 'Respond to an approval request: approve or reject'},
    'decision/respond': {'required': ['decision', 'option'], 'optional': ['text', 'value'],
                         'doc': 'Respond to an open decision item'},
    'proposal/respond': {'required': ['proposal', 'action'], 'optional': ['version', 'policy', 'edit'],
                         'doc': ('Respond to an LLM edit, trial, or enter-work-mode proposal: apply or drop. '
                                 'Applying a stale edit returns 409 and marks it expired; applying a trial checks '
                                 'work mode and wall-time budget before queuing; applying a work request sets policy, '
                                 'optionally applies the accompanying edit, and starts work at version')},
    'run/pause': {'required': ['goal'], 'optional': [],
                  'doc': 'Pause: stop dispatching new tasks while allowing already-started runs to finish'},
    'run/continue': {'required': ['goal'], 'optional': [],
                     'doc': 'Resume: restart stopped or interrupted runs from their latest statepoint'},
    'checkpoint/restore': {'required': ['checkpoint'], 'optional': [],
                           'doc': 'Restore from checkpoint: resume recorded interrupted runs from their statepoints'},
}

NOTIFICATIONS = {
    'goal/stateChanged': 'Goal status, pause flag, stop progress, and budget usage (includes warning at 80%)',
    'task/stateChanged': ('queued, running, stopping, stopped, stop_unconfirmed, interrupted, failed, '
                          'superseded, cancelled, or done'),
    'task/completed': 'Run finished, carrying execution node and k_eff',
    'artifact/registered': 'Content-addressed node registered: paramlist, model, binding, solver_input, run, or report',
    'evidence/updated': 'Evidence head updated: (subject, kind, context) -> verdict',
    'approval/requested': ('Approval record for binding, run, assumed value, param edit, manual override, or resume; '
                           'emitted even under auto-approval with decided_by recording the basis'),
    'decision/added': ('Open decision for rule failure, readback mismatch, geometry check failure, run failure, '
                       'post-interrupt recovery, or acceptance mismatch; duplicate rule failures under the same '
                       'paramlist coalesce and re-emit under the same id'),
    'checkpoint/saved': 'Checkpoint saved: event seq, active runs, and their latest statepoints',
    'output/textDelta': 'Streamed text or reasoning delta',
    'run/progress': 'Batch/cycle index, current k, cumulative k and sigma (carries proposal id for trial runs)',
    'report/published': 'Published report artifact',
    'error': 'Kernel error',
}

METHODS = sorted(list(REQUESTS) + list(NOTIFICATIONS))
assert len(METHODS) == 24


class ProtocolError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
CONFLICT = 409
NOT_FOUND = 404


def check_request(method, params):
    spec = REQUESTS.get(method)
    if spec is None:
        raise ProtocolError(METHOD_NOT_FOUND, f'Unknown method: {method}')
    if not isinstance(params, dict):
        raise ProtocolError(INVALID_PARAMS, 'params must be an object')
    miss = [k for k in spec['required'] if k not in params]
    if miss:
        raise ProtocolError(INVALID_PARAMS, f"{method} missing required params: {', '.join(miss)}")
    extra = [k for k in params if k not in spec['required'] and k not in spec['optional']]
    if extra:
        raise ProtocolError(INVALID_PARAMS, f"{method} received unexpected params: {', '.join(extra)}")
