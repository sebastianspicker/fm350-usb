# shellcheck shell=sh
# at-port.sh - shared AT command tty lookup for the Fibocom FM350-GL / Dell
# DW5931e (USB vendor 0e8d). Sourced (not executed) by install.sh,
# fm350-status and fm350-watchdog; installed to /usr/lib/fm350/at-port.sh.
# POSIX sh/ash (busybox) compatible.
#
# The AT port is USB interface :1.6 (0e8d:7127, mode 41, default) or :1.4
# (0e8d:7126, mode 40); :1.6 is preferred. Only interfaces whose parent USB
# device reports idVendor 0e8d are accepted, so a ttyUSB node of some other
# USB serial adapter that happens to sit on a ":1.6"/":1.4" interface is never
# picked.
#
# FM350_USB_SYSFS overrides the sysfs directory (tests only).

FM350_USB_SYSFS=${FM350_USB_SYSFS:-/sys/bus/usb/devices}

# Prints the /dev/ttyUSBn path of every FM350 AT port candidate, one per line,
# in order of preference.
fm350_at_candidates() {
	for _fm350_suffix in ":1.6" ":1.4"; do
		for _fm350_if in "$FM350_USB_SYSFS"/*"$_fm350_suffix"; do
			[ -d "$_fm350_if" ] || continue
			_fm350_vendor=""
			read -r _fm350_vendor 2>/dev/null <"${_fm350_if%:*}/idVendor" || true
			[ "$_fm350_vendor" = "0e8d" ] || continue
			for _fm350_tty in "$_fm350_if"/ttyUSB*; do
				[ -e "$_fm350_tty" ] || continue
				echo "/dev/${_fm350_tty##*/}"
			done
		done
	done
}

# Prints the preferred AT port; fails (no output) if there is none.
fm350_find_at_device() {
	_fm350_first=$(fm350_at_candidates | head -n 1)
	[ -n "$_fm350_first" ] || return 1
	echo "$_fm350_first"
}

# Succeeds if $1 is one of the FM350 AT port candidates.
fm350_is_at_port() {
	_fm350_cands=$(fm350_at_candidates)
	for _fm350_c in $_fm350_cands; do
		[ "$_fm350_c" = "$1" ] && return 0
	done
	return 1
}
