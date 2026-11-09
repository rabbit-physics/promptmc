# Worker node image for coord-host: OpenMC environment and Reference MC runtime matching worker-linux-1;
# agent.py, node.json, data/, and work/ are mounted at runtime.
FROM ubuntu:24.04
RUN userdel -r ubuntu 2>/dev/null; useradd -m -d /opt/promptmc -u 1001 -s /bin/bash promptmc
ADD openmc-env.tar.gz /
ADD node-refmc-bin.tar.gz /
USER promptmc
WORKDIR /opt/promptmc/pmx-node
CMD ["/opt/promptmc/miniforge3/envs/openmc/bin/python", "-I", "/opt/promptmc/pmx-node/agent.py", "/opt/promptmc/pmx-node/node.json"]
