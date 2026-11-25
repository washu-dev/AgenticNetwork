#!/bin/bash
# Install the vLLM tunnel on the relay VM. Run on the VM as root, from this directory:
#   sudo RELAY_USER=<vm user with the ENGR key> ENGR_LOGIN=<user>@shell.engr.wustl.edu \
#        SSH_KEY=/home/<vm user>/.ssh/id_ed25519 bash install.sh
# Replaces an older vllm-tunnel.service (a fixed-node autossh) in place.
set -euo pipefail
: "${RELAY_USER:?}" "${ENGR_LOGIN:?}" "${SSH_KEY:?}"
install -m 0755 vllm-tunnel.sh vllm-tunnel-check.sh /usr/local/bin/
cat > /etc/default/vllm-tunnel <<CONF
ENGR_LOGIN=${ENGR_LOGIN}
SSH_KEY=${SSH_KEY}
CONF
sed "s/^User=RELAY_USER$/User=${RELAY_USER}/" vllm-tunnel.service > /etc/systemd/system/vllm-tunnel.service
install -m 0644 vllm-tunnel-check.service vllm-tunnel-check.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable vllm-tunnel.service vllm-tunnel-check.timer
systemctl restart vllm-tunnel.service
systemctl start vllm-tunnel-check.timer
systemctl --no-pager status vllm-tunnel.service | head -5
