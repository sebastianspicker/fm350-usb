#!/bin/sh
# Fast host-side APN validation/template test; no router, Docker, or packages.
set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
INSTALL="$SCRIPT_DIR/../install.sh"
test_tmp=$(mktemp -d "${TMPDIR:-/tmp}/fm350-install-input.XXXXXX")
trap 'rm -rf "$test_tmp"' EXIT INT TERM

# A dry run needs package-manager detection and a wan firewall zone. These
# stubs make any attempted package installation or UCI write fail the test.
cat >"$test_tmp/opkg" <<'EOF'
#!/bin/sh
echo "unexpected opkg call: $*" >&2
exit 99
EOF
cat >"$test_tmp/uci" <<'EOF'
#!/bin/sh
if [ "$*" = '-q show firewall' ]; then
	printf "%s\n" "firewall.zone_wan.name='wan'"
	exit 0
fi
case "$*" in
	'-q get '*) exit 1 ;;
esac
echo "unexpected uci call: $*" >&2
exit 99
EOF
chmod 0755 "$test_tmp/opkg" "$test_tmp/uci"

if ! PATH="$test_tmp:$PATH" sh "$INSTALL" --apn 'internet.test_2-5' --dry-run --no-mwan3 --no-watchdog >"$test_tmp/valid.out" 2>&1; then
	cat "$test_tmp/valid.out" >&2
	exit 1
fi
if ! grep -Fq "set network.wwan.apn='internet.test_2-5'" "$test_tmp/valid.out"; then
	echo 'valid APN was not rendered intact' >&2
	exit 1
fi

for bad_apn in 'bad&apn' 'bad|apn' 'bad\apn' "bad'apn" 'bad apn' 'bad;apn' 'bad/apn'; do
	if PATH="$test_tmp:$PATH" sh "$INSTALL" --apn "$bad_apn" --dry-run >"$test_tmp/invalid.out" 2>&1; then
		echo "accepted invalid APN: $bad_apn" >&2
		exit 1
	fi
	if ! grep -Fq 'may contain only letters, digits, dots, underscores, and hyphens' "$test_tmp/invalid.out"; then
		echo "wrong error for invalid APN: $bad_apn" >&2
		cat "$test_tmp/invalid.out" >&2
		exit 1
	fi
done

echo 'install-input-test.sh: PASS'
