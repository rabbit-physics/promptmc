"""Yield policy for Windows compute nodes: decides full-speed vs. efficiency mode and PAUSE file content
from periodic measurements of total CPU usage, WSL usage, and active agent slot count.

Does not call Windows APIs directly: controller.pyw invokes Policy.step every 5 s, and unit tests run
directly on Linux. "Sustained for N s" means every sample over a continuous N-second window satisfies
the condition.

- Default full speed: VM at idle priority, all cores enabled, EcoQoS off, regardless of user presence.
- Enter efficiency mode when total CPU (including WSL) stays above 90% for 30 s: enable EcoQoS and
  restrict VM to efficiency cores. Return to full speed when projected full-speed utilization stays
  below 85% for 120 s with no suspended shards. Projected full-speed utilization =
  other_cpu (excluding WSL) + active_slots / logical_cpus: once back at full speed, each agent thread
  occupies one logical CPU; finished shards are excluded.
- In efficiency mode, when total CPU >= 97% and other CPU >= 60% for 30 s, suspend one quarter of
  the slots active at suspension start (at least 1 slot), and suspend another quarter every 30 s while
  the condition persists; if active slots cannot be read at suspension start, suspend 1 slot per step.
  Resume one step when projected post-resume total CPU (current_total + resumed_slots / logical_cpus)
  stays below 95% for 60 s, then check again every 60 s. Resumed slot count is taken from the agent's
  reported per-level table when available, falling back to nominal step deltas.
  Counting by slots rather than processes ensures a multi-threaded whole-case OpenMC process (2-4 slots)
  releases cores proportional to its thread footprint.
- On battery power, suspend all shards immediately.

Suspension checks total CPU rather than other CPU alone: when 20 foreground processes saturate the
machine, a WSL VM pinned to E-cores still consumes ~28% of total CPU while other CPU plateaus near 70%.
The other >= 60% guard avoids triggering suspension when WSL alone saturates the efficiency cores
(8 E-cores = 40% of 20 logical CPUs), leaving the 12 P-cores uncontended.
"""
import json
import math

ECO_ENTER, ECO_ENTER_S = 0.90, 30       # Enter efficiency mode after total CPU exceeds threshold for duration
ECO_EXIT, ECO_EXIT_S = 0.85, 120        # Exit efficiency mode after projected full-speed CPU stays below threshold
SAT_TOTAL, SAT_OTHER, SAT_S = 0.97, 0.60, 30   # Near-saturation thresholds; suspend another 1/4 every SAT_S
RESUME_TOTAL, CALM_S = 0.95, 60         # Resume one step every CALM_S if projected post-resume CPU < threshold
STEPS = 4


def _since(prev, t, cond):
    """Timestamp when cond began holding continuously, or None if cond is False."""
    if not cond:
        return None
    return t if prev is None else prev


class Policy:
    def __init__(self, ncpu=20):
        self.ncpu = max(1, ncpu)    # Logical CPU count: one slot occupies one logical CPU at full speed
        self.mode = 'full'
        self.level = 0              # Number of quarters suspended (0..STEPS)
        self.r0 = None              # Active slots when suspension began; quarters are computed against this
        self.slots = None           # Most recent reading of active slots
        self.high_since = self.low_since = self.sat_since = self.calm_since = None
        self.projected = None       # Most recent projected full-speed utilization

    def step(self, t, total, wsl, ac=True, running=None):
        """Evaluate one policy tick.

        t: timestamp (s); total: whole-host CPU fraction [0, 1] incl. WSL; wsl: vmmemWSL CPU [0, 1];
        ac: True if on AC power; running: active slot count, agent state dict, or callable polled in eco mode.
        Returns (mode, pause) where mode is 'full' or 'eco' and pause is the PAUSE text ('' for none).
        """
        other = max(0.0, total - wsl)
        if self.mode == 'full':
            self.projected = total
            self.high_since = _since(self.high_since, t, total > ECO_ENTER)
            if self.high_since is not None and t - self.high_since >= ECO_ENTER_S:
                self.mode = 'eco'
                self.low_since = self.sat_since = self.calm_since = None
        else:
            r = running() if callable(running) else running
            state = r if isinstance(r, dict) else {}
            if state:
                r = state.get('compute_slots', state.get('slots_used', state.get('running')))
            if r is not None:
                self.slots = r
            # Fall back to current WSL usage if active slot count has not yet been read
            self.projected = other + (self.slots / self.ncpu if self.slots is not None else wsl)
            self.low_since = _since(self.low_since, t, self.projected < ECO_EXIT)
            self.sat_since = _since(self.sat_since, t, total >= SAT_TOTAL and other >= SAT_OTHER)
            resume = state.get('resume_slots', {}).get(str(self._target(self.level - 1)), self._step_slots())
            after = total + resume / self.ncpu
            self.calm_since = _since(self.calm_since, t, self.level > 0 and after < RESUME_TOTAL)
            if self.sat_since is not None and t - self.sat_since >= SAT_S and self.level < STEPS:
                if self.r0 is None:
                    self.r0 = self.slots
                self.level += 1
                self.sat_since = t
                self.calm_since = None
            elif self.calm_since is not None and t - self.calm_since >= CALM_S and self.level > 0:
                self.level -= 1
                self.calm_since = t if self.level > 0 else None
                if self.level == 0:
                    self.r0 = None
            if self.level == 0 and self.low_since is not None and t - self.low_since >= ECO_EXIT_S:
                self.mode = 'full'
                self.high_since = None
        return self.mode, self.pause(ac)

    def _target(self, level):
        """Target slot count to suspend at a given level; None means suspend all."""
        if level <= 0:
            return 0
        if level >= STEPS:
            return None
        if self.r0 is None:
            return level
        return max(1, math.ceil(level * self.r0 / STEPS))

    def _step_slots(self):
        """Number of slots released when stepping down one suspension level."""
        if self.level <= 0:
            return 0
        hi = self._target(self.level)
        if hi is None:
            hi = max(self.r0 or 0, self.slots or 0, self._target(STEPS - 1) or 0)
        return max(0, hi - self._target(self.level - 1))

    def suspended_target(self):
        """Target slots to suspend; None means all."""
        return self._target(self.level)

    def pause(self, ac=True):
        if not ac:
            return 'suspend'
        n = self.suspended_target()
        if n == 0:
            return ''
        return 'suspend' if n is None else f'suspend {n}'


def used_slots(text):
    """Parse agent.json text -> active slot count (falls back to running shards if slots_used is absent)."""
    try:
        st = json.loads(text)
        return int(st['slots_used'] if 'slots_used' in st else st['running'])
    except (ValueError, TypeError, KeyError):
        return None
