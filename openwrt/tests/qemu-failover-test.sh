#!/bin/sh
# qemu-failover-test.sh - end-to-end failover test: boots real OpenWrt
# 24.10 in QEMU (aarch64, HVF-accelerated), applies our installer's
# mwan3/firewall config, and proves LAN traffic fails over from "wan" to a
# stand-in "wwan" when wan dies, and fails back when wan returns. No modem
# needed: wwan is a plain DHCP uplink instead of the real atc/FM350 proto.
#
# Runs on the development host (macOS/arm64, needs qemu-system-aarch64
# -accel hvf, qemu-img, socat, nc -U, ssh), not on the router. POSIX sh.
#
# --- Architecture -----------------------------------------------------------
#
# Guest NICs (all virtio-net-pci):
#   eth0 = lan (br-lan, stock OpenWrt default config, UNTOUCHED). Backed by
#          a QEMU "socket" netdev with no peer, so it carries no traffic of
#          its own: it only exists to satisfy br-lan's port list. The real
#          LAN traffic comes from a netns ("lanclient") plugged into br-lan
#          via a veth pair (see qemu-failover/provision.sh), which traverses
#          mwan3 policy routing exactly like a real LAN host would.
#   eth1 = wan (stock network.wan, proto dhcp). QEMU slirp user network with
#          its own subnet -> real internet via the host, so mwan3's
#          track_ip pings (1.1.1.1, 9.9.9.9) genuinely succeed.
#   eth2 = wwan (installer's atc config, immediately overridden by
#          provision.sh to proto dhcp: a stand-in cellular uplink). Its own
#          QEMU slirp subnet -> also real internet, independent of eth1's.
#   eth3 = mgmt, test-harness only: not part of install.sh's config, not in
#          mwan3, not in the wan firewall zone. Exists purely so the host
#          can SSH into the router to drive the rest of the test (see
#          qemu-failover/mgmt-bootstrap.sh for why eth0/lan isn't reused for
#          this: bridging QEMU's slirp DHCP server onto br-lan made it race
#          dnsmasq for the LAN client's lease, which then bypassed the
#          router's mwan3 routing entirely).
#
# Failure injection, two distinct methods (mwan3 notices each differently):
#   - "wan down": QEMU monitor `set_link net1 off` on the wan netdev. This
#     drops carrier, so netifd's hotplug ifdown fires (typically fast).
#   - "upstream dead" (link up, packets dropped): an nft rule inside the
#     guest drops all egress on the given interface, including mwan3's own
#     track_ip pings, without touching carrier state. Only mwan3's
#     ping-based interval/down tracking can notice this path.
#
# Egress proof: an nft table (egresstest) inside the guest counts packets
# with a dedicated destination (8.8.8.8, never used as an mwan3 track_ip) by
# output interface (eth1 vs eth2). This is deliberately not "check the route
# table": it counts packets that actually left the kernel via each NIC.
#
# Every wait is bounded, with the bound computed from uci/mwan3.uci's own
# interval/down/up values (see bound_* below) plus a fixed slack, printed
# before use. On any failure, mwan3 status / ip rule / ip route show table
# all / logread are dumped from the VM.
#
# Deliberately no `set -e`: most of this script's own control flow relies on
# remote_run() legitimately returning non-zero (failed pings during the
# "both down" assertion, convergence polling, etc.), which `set -e` would
# turn into a premature exit for any bare (non-conditional) invocation.
# Every step that must not be allowed to fail silently is checked explicitly
# instead.

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
OPENWRT_DIR=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)
HELPERS_DIR="$SCRIPT_DIR/qemu-failover"
CACHE_DIR="$SCRIPT_DIR/.cache"

# OpenWrt release pinned to a specific point release for reproducibility.
# Re-check https://downloads.openwrt.org/releases/ if this disappears.
OPENWRT_VER=24.10.8
IMG_BASENAME="openwrt-$OPENWRT_VER-armsr-armv8-generic-ext4-combined-efi.img.gz"
DL_BASE="https://downloads.openwrt.org/releases/$OPENWRT_VER/targets/armsr/armv8"

APN=internet.telekom
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5"

status=0
fail() {
	echo "FAIL: $*" >&2
	status=1
}

log() { echo "qemu-failover-test.sh: $*"; }

# --- prerequisites -----------------------------------------------------------
for tool in qemu-system-aarch64 qemu-img socat nc ssh ssh-keygen curl shasum; do
	command -v "$tool" >/dev/null 2>&1 || {
		echo "qemu-failover-test.sh: required tool '$tool' not found in PATH" >&2
		exit 1
	}
done

QEMU_PREFIX=$(brew --prefix qemu 2>/dev/null) || {
	echo "qemu-failover-test.sh: 'brew --prefix qemu' failed, is qemu installed via Homebrew?" >&2
	exit 1
}
CODE_FD="$QEMU_PREFIX/share/qemu/edk2-aarch64-code.fd"
[ -f "$CODE_FD" ] || {
	echo "qemu-failover-test.sh: EDK2 firmware not found at $CODE_FD" >&2
	exit 1
}

echo "qemu-failover-test.sh: shellcheck"
if command -v shellcheck >/dev/null 2>&1; then
	shellcheck -s sh \
		"$SCRIPT_DIR/qemu-failover-test.sh" \
		"$HELPERS_DIR/provision.sh" \
		"$HELPERS_DIR/mgmt-bootstrap.sh" \
		"$HELPERS_DIR/remote-helpers.sh" || fail "shellcheck reported issues"
else
	echo "qemu-failover-test.sh: shellcheck not installed, skipping static check" >&2
fi

# --- mwan3.uci-derived timing bounds ------------------------------------------
# interval x down/up + one extra interval + a fixed slack for hotplug/ssh
# round-trip overhead. Printed before each wait that uses them.
MWAN3_UCI="$OPENWRT_DIR/uci/mwan3.uci"
SLACK=10
wan_interval=$(sed -n "s/^set mwan3\.wan\.interval='\([0-9]*\)'$/\1/p" "$MWAN3_UCI")
wan_down=$(sed -n "s/^set mwan3\.wan\.down='\([0-9]*\)'$/\1/p" "$MWAN3_UCI")
wan_up=$(sed -n "s/^set mwan3\.wan\.up='\([0-9]*\)'$/\1/p" "$MWAN3_UCI")
wwan_interval=$(sed -n "s/^set mwan3\.wwan\.interval='\([0-9]*\)'$/\1/p" "$MWAN3_UCI")
wwan_down=$(sed -n "s/^set mwan3\.wwan\.down='\([0-9]*\)'$/\1/p" "$MWAN3_UCI")
wwan_up=$(sed -n "s/^set mwan3\.wwan\.up='\([0-9]*\)'$/\1/p" "$MWAN3_UCI")
for v in wan_interval wan_down wan_up wwan_interval wwan_down wwan_up; do
	eval "[ -n \"\$$v\" ]" || {
		echo "qemu-failover-test.sh: could not parse $v from $MWAN3_UCI" >&2
		exit 1
	}
done
bound_wan_down=$((wan_down * wan_interval + wan_interval + SLACK))
bound_wan_up=$((wan_up * wan_interval + wan_interval + SLACK))
bound_wwan_down=$((wwan_down * wwan_interval + wwan_interval + SLACK))
bound_wwan_up=$((wwan_up * wwan_interval + wwan_interval + SLACK))
bound_both_down=$bound_wwan_down
[ "$bound_wan_down" -gt "$bound_both_down" ] && bound_both_down=$bound_wan_down
bound_both_up=$bound_wwan_up
[ "$bound_wan_up" -gt "$bound_both_up" ] && bound_both_up=$bound_wan_up

log "timing bounds: wan down<=${bound_wan_down}s up<=${bound_wan_up}s, wwan down<=${bound_wwan_down}s up<=${bound_wwan_up}s (interval x down/up + interval + ${SLACK}s slack, from uci/mwan3.uci)"

# --- fetch/verify the base image ---------------------------------------------
mkdir -p "$CACHE_DIR"
[ -f "$CACHE_DIR/.gitignore" ] || echo '*' >"$CACHE_DIR/.gitignore"

fetch_base_image() {
	img_gz="$CACHE_DIR/$IMG_BASENAME"
	sums="$CACHE_DIR/sha256sums"
	if [ ! -f "$img_gz" ]; then
		log "downloading $IMG_BASENAME"
		curl -sL -o "$img_gz.tmp" "$DL_BASE/$IMG_BASENAME"
		mv "$img_gz.tmp" "$img_gz"
	fi
	if [ ! -f "$sums" ]; then
		curl -sL -o "$sums.tmp" "$DL_BASE/sha256sums"
		mv "$sums.tmp" "$sums"
	fi
	expected=$(grep " \*\{0,1\}$IMG_BASENAME\$" "$sums" | awk '{print $1}')
	[ -n "$expected" ] || {
		echo "qemu-failover-test.sh: $IMG_BASENAME not listed in sha256sums" >&2
		exit 1
	}
	actual=$(shasum -a 256 "$img_gz" | awk '{print $1}')
	if [ "$actual" != "$expected" ]; then
		echo "qemu-failover-test.sh: checksum mismatch for $IMG_BASENAME (expected $expected, got $actual)" >&2
		exit 1
	fi
	if [ ! -f "$CACHE_DIR/base.raw" ]; then
		log "extracting base.raw from $IMG_BASENAME"
		gunzip -k -c "$img_gz" >"$CACHE_DIR/base.raw.tmp" 2>/dev/null || true
		[ -s "$CACHE_DIR/base.raw.tmp" ] || {
			echo "qemu-failover-test.sh: gunzip produced an empty base.raw" >&2
			exit 1
		}
		mv "$CACHE_DIR/base.raw.tmp" "$CACHE_DIR/base.raw"
	fi
}
fetch_base_image

# --- per-run workdir and cleanup ----------------------------------------------
WORK=$(mktemp -d "${TMPDIR:-/tmp}/qemu-failover-test.XXXXXX")
QEMU_PID=""
SOCAT_PID=""
SERIAL_READER_PID=""

# shellcheck disable=SC2317,SC2329 # invoked indirectly via the trap below
cleanup() {
	[ -n "$QEMU_PID" ] && kill "$QEMU_PID" >/dev/null 2>&1
	[ -n "$SOCAT_PID" ] && kill "$SOCAT_PID" >/dev/null 2>&1
	[ -n "$SERIAL_READER_PID" ] && kill "$SERIAL_READER_PID" >/dev/null 2>&1
	# give qemu a moment to release the overlay before we remove it
	sleep 1
	rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

dump_diagnostics() {
	echo "--- diagnostics ---" >&2
	remote_run "mwan3 status; echo ---ip-rule---; ip rule; echo ---ip-route---; ip route show table all; echo ---logread---; logread | tail -n 100" >&2 || true
	echo "--- serial.log (tail) ---" >&2
	tail -c 4000 "$WORK/serial.log" 2>/dev/null >&2 || true
	echo "--- qemu.log ---" >&2
	cat "$WORK/qemu.log" 2>/dev/null >&2 || true
}

# --- VM lifecycle --------------------------------------------------------------
SSH_PORT=$((20000 + (($$ + $(date +%s)) % 10000)))
MCAST_PORT=$((30000 + ($$ % 10000)))

start_vm() {
	qemu-img create -f qcow2 -F raw -b "$CACHE_DIR/base.raw" "$WORK/overlay.qcow2" >/dev/null
	qemu-img resize "$WORK/overlay.qcow2" 1G >/dev/null
	dd if=/dev/zero of="$WORK/vars.fd" bs=1048576 count=64 status=none
	qemu-system-aarch64 \
		-M virt -accel hvf -cpu host -smp 2 -m 512 \
		-drive if=pflash,format=raw,file="$CODE_FD",readonly=on \
		-drive if=pflash,format=raw,file="$WORK/vars.fd" \
		-drive if=virtio,format=qcow2,file="$WORK/overlay.qcow2" \
		-netdev socket,id=net0,mcast=230.10.10.10:"$MCAST_PORT" -device virtio-net-pci,netdev=net0 \
		-netdev user,id=net1,net=10.40.11.0/24,dhcpstart=10.40.11.10 -device virtio-net-pci,netdev=net1 \
		-netdev user,id=net2,net=10.40.22.0/24,dhcpstart=10.40.22.10 -device virtio-net-pci,netdev=net2 \
		-netdev user,id=net3,net=10.40.33.0/24,dhcpstart=10.40.33.10,hostfwd=tcp:127.0.0.1:"$SSH_PORT"-10.40.33.10:22 -device virtio-net-pci,netdev=net3 \
		-serial unix:"$WORK/serial.sock",server,nowait \
		-monitor unix:"$WORK/mon.sock",server,nowait \
		-display none \
		>"$WORK/qemu.log" 2>&1 &
	QEMU_PID=$!
}

start_serial_bridge() {
	socat -d -d pty,raw,echo=0,link="$WORK/console.pty" UNIX-CONNECT:"$WORK/serial.sock" >"$WORK/socat.log" 2>&1 &
	SOCAT_PID=$!
	i=0
	while [ ! -e "$WORK/console.pty" ] && [ "$i" -lt 10 ]; do
		sleep 1
		i=$((i + 1))
	done
	[ -e "$WORK/console.pty" ] || return 1
	: >"$WORK/serial.log"
	cat "$WORK/console.pty" >>"$WORK/serial.log" 2>&1 &
	SERIAL_READER_PID=$!
	return 0
}

serial_send() { printf '%s\n' "$1" >"$WORK/console.pty"; }

wait_boot() {
	timeout_s=$1
	: >"$WORK/serial.log"
	elapsed=0
	while [ "$elapsed" -lt "$timeout_s" ]; do
		serial_send ""
		sleep 2
		grep -q 'root@OpenWrt:~# *$' "$WORK/serial.log" && return 0
		elapsed=$((elapsed + 2))
	done
	return 1
}

# serial_run CMD TIMEOUT_S - types a (possibly multi-line) command blob at
# the live serial console and waits for a completion marker + exit code.
serial_run() {
	cmd=$1
	timeout_s=$2
	: >"$WORK/serial.log"
	printf '%s\n' "$cmd" >"$WORK/console.pty"
	printf 'echo QFT_SERIAL_DONE:$?\n' >"$WORK/console.pty"
	elapsed=0
	while [ "$elapsed" -lt "$timeout_s" ]; do
		if grep -q '^QFT_SERIAL_DONE:' "$WORK/serial.log"; then
			ec=$(grep '^QFT_SERIAL_DONE:' "$WORK/serial.log" | tail -1 | sed 's/^QFT_SERIAL_DONE://' | tr -d '\r')
			return "$ec"
		fi
		sleep 2
		elapsed=$((elapsed + 2))
	done
	return 124
}

monitor_cmd() {
	printf '%s\n' "$1" | nc -U -w2 "$WORK/mon.sock" >/dev/null 2>&1
}

ssh_wait_ready() {
	timeout_s=$1
	elapsed=0
	while [ "$elapsed" -lt "$timeout_s" ]; do
		# shellcheck disable=SC2086 # SSH_OPTS is an intentional word-split option list
		if printf '\n' | ssh $SSH_OPTS -p "$SSH_PORT" root@127.0.0.1 true >/dev/null 2>&1; then
			return 0
		fi
		sleep 2
		elapsed=$((elapsed + 2))
	done
	return 1
}

# ssh_bootstrap_key generates a host-side keypair, installs the public half
# on the guest via the initial password (blank-root-password) auth, then
# switches SSH_OPTS to key auth for the rest of the run: this frees up
# stdin (needed for tar payloads in remote_copy) instead of it having to
# carry an empty line for password auth on every single call.
ssh_bootstrap_key() {
	ssh-keygen -t ed25519 -N '' -f "$WORK/id_ed25519" -q
	pubkey=$(cat "$WORK/id_ed25519.pub")
	# shellcheck disable=SC2086 # SSH_OPTS is an intentional word-split option list
	printf '\n' | ssh $SSH_OPTS -p "$SSH_PORT" root@127.0.0.1 \
		"mkdir -p /etc/dropbear /root/.ssh && echo '$pubkey' >>/etc/dropbear/authorized_keys && echo '$pubkey' >>/root/.ssh/authorized_keys && chmod 600 /etc/dropbear/authorized_keys /root/.ssh/authorized_keys" \
		>/dev/null || return 1
	SSH_OPTS="$SSH_OPTS -i $WORK/id_ed25519 -o BatchMode=yes"
}

# remote_run CMD - runs CMD as a single /bin/sh -c argument over SSH,
# printing only the command's own stdout+stderr (the login banner is
# stripped via markers) and returning its exit code.
remote_run() {
	cmd=$1
	# shellcheck disable=SC2086 # SSH_OPTS is an intentional word-split option list
	out=$(ssh $SSH_OPTS -p "$SSH_PORT" root@127.0.0.1 "echo QFT_BEGIN; $cmd; echo QFT_END:\$?" 2>&1)
	printf '%s\n' "$out" | sed -n '/^QFT_BEGIN$/,/^QFT_END:/{/^QFT_BEGIN$/d;/^QFT_END:/d;p;}'
	ec_line=$(printf '%s\n' "$out" | grep '^QFT_END:' | tail -1)
	ec=${ec_line#QFT_END:}
	case $ec in
	'' | *[!0-9]*) ec=1 ;;
	esac
	return "$ec"
}

# remote_copy BASEDIR DEST PATH... - tars PATH... (relative to BASEDIR) and
# extracts it into DEST on the guest.
remote_copy() {
	basedir=$1
	dest=$2
	shift 2
	# shellcheck disable=SC2086 # SSH_OPTS is an intentional word-split option list
	(cd "$basedir" && COPYFILE_DISABLE=1 tar -cf - "$@") |
		ssh $SSH_OPTS -p "$SSH_PORT" root@127.0.0.1 "mkdir -p '$dest' && tar -C '$dest' -xf -"
}

# --- boot + bring up mgmt over serial -----------------------------------------
log "starting QEMU (ssh port $SSH_PORT, mcast port $MCAST_PORT)"
start_vm
sleep 2 # let qemu create its unix sockets before socat dials them
start_serial_bridge || {
	echo "qemu-failover-test.sh: serial console did not come up" >&2
	cat "$WORK/socat.log" >&2 2>/dev/null || true
	exit 1
}

log "waiting for first boot (up to 120s)"
wait_boot 120 || {
	echo "qemu-failover-test.sh: OpenWrt did not reach a login prompt within 120s" >&2
	tail -c 4000 "$WORK/serial.log" >&2 || true
	exit 1
}

log "bringing up the mgmt NIC over serial"
serial_run "$(cat "$HELPERS_DIR/mgmt-bootstrap.sh")" 30
mgmt_rc=$?
if [ "$mgmt_rc" -ne 0 ]; then
	echo "qemu-failover-test.sh: mgmt-bootstrap.sh failed over serial (rc=$mgmt_rc)" >&2
	tail -c 4000 "$WORK/serial.log" >&2 || true
	exit 1
fi

log "waiting for SSH on the mgmt NIC (up to 30s)"
ssh_wait_ready 30 || {
	echo "qemu-failover-test.sh: SSH did not come up on the mgmt NIC" >&2
	tail -c 4000 "$WORK/serial.log" >&2 || true
	exit 1
}

log "installing a test SSH key (frees stdin for file transfers)"
ssh_bootstrap_key || {
	echo "qemu-failover-test.sh: failed to install the test SSH key" >&2
	exit 1
}

# --- provisioning --------------------------------------------------------------
log "copying openwrt/ into the VM"
remote_copy "$OPENWRT_DIR" /root/openwrt install.sh uninstall.sh fm350-status.sh uci files
remote_copy "$SCRIPT_DIR" /root/openwrt qemu-failover

log "provisioning: opkg install mwan3, run install.sh, override wwan, build LAN client"
remote_run "sh /root/openwrt/qemu-failover/provision.sh $APN"
provision_rc=$?
if [ "$provision_rc" -ne 0 ]; then
	echo "qemu-failover-test.sh: provision.sh failed (rc=$provision_rc)" >&2
	dump_diagnostics
	exit 1
fi

# --- assertions ------------------------------------------------------------
helpers=". /root/openwrt/qemu-failover/remote-helpers.sh"

log "=== assertion (a): baseline ==="
remote_run "mwan3 status" >"$WORK/status-baseline.txt" || true
if ! grep -qE 'interface wan is online' "$WORK/status-baseline.txt"; then
	fail "baseline: wan is not online per mwan3 status"
fi
if ! grep -qE 'interface wwan is online' "$WORK/status-baseline.txt"; then
	fail "baseline: wwan is not online per mwan3 status"
fi
remote_run "$helpers; egress_reset"
remote_run "$helpers; lan_ping 8.8.8.8 5 2" >/dev/null
lan_ping_rc=$?
[ "$lan_ping_rc" -eq 0 ] || fail "baseline: LAN client could not ping 8.8.8.8"
sleep 1
wan_n=$(remote_run "$helpers; egress_read egress_wan")
wwan_n=$(remote_run "$helpers; egress_read egress_wwan")
log "baseline egress: eth1(wan)=$wan_n eth2(wwan)=$wwan_n packets to 8.8.8.8"
if [ "${wan_n:-0}" -lt 3 ] || [ "${wwan_n:-0}" -ne 0 ]; then
	fail "baseline: LAN client traffic did not egress via wan only (wan=$wan_n wwan=$wwan_n)"
fi

log "=== assertion (b): wan down (set_link) -> failover to wwan (bound ${bound_wan_down}s) ==="
t0=$(date +%s)
monitor_cmd "set_link net1 off"
converged=0
elapsed=0
while [ "$elapsed" -lt "$bound_wan_down" ]; do
	remote_run "$helpers; wait_failover_member wwan 1" >/dev/null 2>&1 && {
		converged=1
		break
	}
	sleep 2
	elapsed=$((elapsed + 2))
done
t1=$(date +%s)
if [ "$converged" -eq 1 ]; then
	log "measured wan->wwan failover time: $((t1 - t0))s (bound ${bound_wan_down}s)"
else
	fail "wan down: failover policy did not switch to wwan within ${bound_wan_down}s"
	dump_diagnostics
fi
remote_run "$helpers; lan_ping 8.8.8.8 3 3" >/dev/null ||
	fail "wan down: LAN client lost connectivity during failover"
remote_run "$helpers; egress_reset"
remote_run "$helpers; lan_ping 8.8.8.8 5 2" >/dev/null
wan_n=$(remote_run "$helpers; egress_read egress_wan")
wwan_n=$(remote_run "$helpers; egress_read egress_wwan")
log "post-failover egress: eth1(wan)=$wan_n eth2(wwan)=$wwan_n packets to 8.8.8.8"
if [ "${wwan_n:-0}" -lt 3 ] || [ "${wan_n:-0}" -ne 0 ]; then
	fail "wan down: LAN client traffic did not egress via wwan only (wan=$wan_n wwan=$wwan_n)"
fi

log "=== assertion (c): wan back up (set_link) -> failback to wan (bound ${bound_wan_up}s) ==="
t0=$(date +%s)
monitor_cmd "set_link net1 on"
converged=0
elapsed=0
while [ "$elapsed" -lt "$bound_wan_up" ]; do
	remote_run "$helpers; wait_failover_member wan 1" >/dev/null 2>&1 && {
		converged=1
		break
	}
	sleep 2
	elapsed=$((elapsed + 2))
done
t1=$(date +%s)
if [ "$converged" -eq 1 ]; then
	log "measured wwan->wan failback time: $((t1 - t0))s (bound ${bound_wan_up}s)"
else
	fail "wan up: failover policy did not switch back to wan within ${bound_wan_up}s"
	dump_diagnostics
fi
remote_run "$helpers; egress_reset"
remote_run "$helpers; lan_ping 8.8.8.8 5 2" >/dev/null
wan_n=$(remote_run "$helpers; egress_read egress_wan")
wwan_n=$(remote_run "$helpers; egress_read egress_wwan")
log "post-failback egress: eth1(wan)=$wan_n eth2(wwan)=$wwan_n packets to 8.8.8.8"
if [ "${wan_n:-0}" -lt 3 ] || [ "${wwan_n:-0}" -ne 0 ]; then
	fail "wan up: LAN client traffic did not return to wan only (wan=$wan_n wwan=$wwan_n)"
fi

log "=== assertion (d): upstream dead on wan (link stays up, nft drop) -> failover (bound ${bound_wan_down}s) ==="
t0=$(date +%s)
remote_run "$helpers; fail_link_dead eth1" || fail "upstream dead: fail_link_dead eth1 itself failed"
converged=0
elapsed=0
while [ "$elapsed" -lt "$bound_wan_down" ]; do
	remote_run "$helpers; wait_failover_member wwan 1" >/dev/null 2>&1 && {
		converged=1
		break
	}
	sleep 2
	elapsed=$((elapsed + 2))
done
t1=$(date +%s)
if [ "$converged" -eq 1 ]; then
	log "measured wan-upstream-dead->wwan failover time: $((t1 - t0))s (bound ${bound_wan_down}s)"
else
	fail "upstream dead: failover policy did not switch to wwan within ${bound_wan_down}s"
	dump_diagnostics
fi
remote_run "$helpers; lan_ping 8.8.8.8 3 3" >/dev/null ||
	fail "upstream dead: LAN client lost connectivity during failover"

log "=== assertion (d continued): restore wan -> failback (bound ${bound_wan_up}s) ==="
t0=$(date +%s)
remote_run "$helpers; fail_link_restore" || fail "upstream restore: fail_link_restore itself failed"
converged=0
elapsed=0
while [ "$elapsed" -lt "$bound_wan_up" ]; do
	remote_run "$helpers; wait_failover_member wan 1" >/dev/null 2>&1 && {
		converged=1
		break
	}
	sleep 2
	elapsed=$((elapsed + 2))
done
t1=$(date +%s)
if [ "$converged" -eq 1 ]; then
	log "measured upstream-restored->wan failback time: $((t1 - t0))s (bound ${bound_wan_up}s)"
else
	fail "upstream restore: failover policy did not switch back to wan within ${bound_wan_up}s"
	dump_diagnostics
fi

log "=== assertion (e): both wan and wwan down -> last_resort unreachable (bound ${bound_both_down}s) ==="
monitor_cmd "set_link net1 off"
monitor_cmd "set_link net2 off"
converged=0
elapsed=0
while [ "$elapsed" -lt "$bound_both_down" ]; do
	remote_run "$helpers; wait_failover_unreachable 1" >/dev/null 2>&1 && {
		converged=1
		break
	}
	sleep 2
	elapsed=$((elapsed + 2))
done
if [ "$converged" -eq 1 ]; then
	log "both wan and wwan down: failover policy reports unreachable, as expected"
else
	fail "both down: failover policy never reported unreachable within ${bound_both_down}s"
	dump_diagnostics
fi
remote_run "$helpers; lan_ping 8.8.8.8 3 2" >/dev/null
lan_ping_rc=$?
if [ "$lan_ping_rc" -eq 0 ]; then
	fail "both down: LAN client ping unexpectedly succeeded"
else
	log "LAN client ping correctly failed (bounded, no hang) with both uplinks down"
fi
remote_run "ip route show table all | grep -i unreach" >"$WORK/unreachable-route.txt" 2>&1 || true
log "unreachable routes in the VM (diagnostic only; the fdxx::/48 one is OpenWrt's default ULA route, not mwan3): $(cat "$WORK/unreachable-route.txt")"

log "=== restoring both uplinks (bound ${bound_both_up}s) ==="
monitor_cmd "set_link net1 on"
monitor_cmd "set_link net2 on"
converged=0
elapsed=0
while [ "$elapsed" -lt "$bound_both_up" ]; do
	remote_run "$helpers; wait_failover_member wan 1" >/dev/null 2>&1 && {
		converged=1
		break
	}
	sleep 2
	elapsed=$((elapsed + 2))
done
if [ "$converged" -eq 1 ]; then
	log "both uplinks restored, failover policy back on wan"
else
	fail "both down: failover policy did not recover to wan within ${bound_both_up}s"
	dump_diagnostics
fi

log "=== assertion (f): router-originated traffic ==="
remote_run "$helpers; egress_reset"
remote_run "ping -c3 -W2 8.8.8.8" >/dev/null
router_ping_rc=$?
sleep 1
wan_n=$(remote_run "$helpers; egress_read egress_wan")
wwan_n=$(remote_run "$helpers; egress_read egress_wwan")
log "router-originated ping to 8.8.8.8: exit=$router_ping_rc, egress eth1(wan)=$wan_n eth2(wwan)=$wwan_n"
if [ "$router_ping_rc" -eq 0 ] && [ "${wan_n:-0}" -ge 1 ]; then
	log "observed: router-originated traffic egresses wan, the active member (not tested: whether it follows a failover to wwan)"
elif [ "$router_ping_rc" -eq 0 ]; then
	log "observed: router-originated traffic reached the internet but not via eth1/eth2 as tracked (mwan3 globals may exempt local traffic); reported as-is, not treated as a failure"
else
	fail "router-originated traffic: ping to 8.8.8.8 from the router itself failed even with both uplinks healthy"
fi

if [ "$status" -eq 0 ]; then
	echo "qemu-failover-test.sh: PASS"
else
	echo "qemu-failover-test.sh: FAIL, see output above" >&2
fi

exit "$status"
