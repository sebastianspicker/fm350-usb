"""Packaging placeholder only.

The actual helper daemon lives in ``fm350mac_helper.py`` next to this file,
and is deliberately **not** imported here (or anywhere else in the
``fm350mac`` package): it is a standalone, stdlib-only, Python 3.9
compatible script that gets copied verbatim to
``/usr/local/libexec/fm350mac-helper`` and run by the system
``/usr/bin/python3`` as root (see docs/macos-driver.md, "Privilege
separation"). Nothing in the main package may depend on it at runtime;
``cli.py``'s ``helper install`` reads it as a file, not a module.
"""
