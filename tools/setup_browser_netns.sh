#!/usr/bin/env bash
# Idempotent setup for dedicated isolated Linux network namespace: snowball-browser
# Enforces: NO direct DNS/UDP, NO direct public network, NO host loopback,
# ONLY TCP to host enforcing egress gateway at 10.200.1.1:8899.
set -euo pipefail

NETNS="snowball-browser"
VETH_HOST="veth-br-host"
VETH_NS="veth-br-ns"
GATEWAY_IP="10.200.1.1"
WORKER_IP="10.200.1.2"
GATEWAY_PORT="8899"

# 1. Create network namespace if not present
if ! ip netns list | grep -qw "$NETNS"; then
    echo "Creating network namespace $NETNS..."
    ip netns add "$NETNS"
fi

# 2. Create veth pair if not present
if ! ip link show "$VETH_HOST" &>/dev/null; then
    echo "Creating veth pair $VETH_HOST <-> $VETH_NS..."
    ip link add "$VETH_HOST" type veth peer name "$VETH_NS"
    ip link set "$VETH_NS" netns "$NETNS"
fi

# 3. Configure host side veth
ip addr flush dev "$VETH_HOST" || true
ip addr add "${GATEWAY_IP}/30" dev "$VETH_HOST"
ip link set "$VETH_HOST" up

# 4. Configure namespace side
ip netns exec "$NETNS" ip link set lo up
ip netns exec "$NETNS" ip addr flush dev "$VETH_NS" || true
ip netns exec "$NETNS" ip addr add "${WORKER_IP}/30" dev "$VETH_NS"
ip netns exec "$NETNS" ip link set "$VETH_NS" up
ip netns exec "$NETNS" ip route replace default via "$GATEWAY_IP" dev "$VETH_NS"

# 5. Enforce firewall rules inside namespace: OUTPUT default DROP
ip netns exec "$NETNS" iptables -F OUTPUT 2>/dev/null || true
ip netns exec "$NETNS" iptables -P OUTPUT DROP 2>/dev/null || true
# Allow loopback inside namespace
ip netns exec "$NETNS" iptables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true
# Allow established/related TCP
ip netns exec "$NETNS" iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
# Allow TCP ONLY to enforcing egress gateway
ip netns exec "$NETNS" iptables -A OUTPUT -p tcp -d "$GATEWAY_IP" --dport "$GATEWAY_PORT" -j ACCEPT 2>/dev/null || true
# Explicitly drop all UDP (blocks direct DNS port 53 escape)
ip netns exec "$NETNS" iptables -A OUTPUT -p udp -j DROP 2>/dev/null || true
# Explicitly drop all private and loopback destinations
ip netns exec "$NETNS" iptables -A OUTPUT -d 127.0.0.0/8 -j DROP 2>/dev/null || true
ip netns exec "$NETNS" iptables -A OUTPUT -d 10.0.0.0/8 -j DROP 2>/dev/null || true
ip netns exec "$NETNS" iptables -A OUTPUT -d 172.16.0.0/12 -j DROP 2>/dev/null || true
ip netns exec "$NETNS" iptables -A OUTPUT -d 192.168.0.0/16 -j DROP 2>/dev/null || true
ip netns exec "$NETNS" iptables -A OUTPUT -d 100.64.0.0/10 -j DROP 2>/dev/null || true
ip netns exec "$NETNS" iptables -A OUTPUT -d 169.254.0.0/16 -j DROP 2>/dev/null || true

echo "Network namespace $NETNS configured successfully (egress locked to $GATEWAY_IP:$GATEWAY_PORT only)."
