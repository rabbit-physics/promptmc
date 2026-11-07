"""Compute nodes: coordinator (coord), worker agent (agent), job sharding & merging (jobs),
and coordinator admin API client (client).

coord.py, agent.py, and ctl.py can run as standalone scripts (`python3 -I p1/nodes/coord.py`),
relying only on the Python standard library; agent.py is a single self-contained file copied to each node.
"""
