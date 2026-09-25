"""AsyncEndpoint/EventLoop pool and lifetime tests, with a fake Libusb that
records submit/cancel/free calls and lets the test fire a transfer's
callback with whatever status it likes -- exactly what libusb itself would
do, minus the real syscalls. No real USB hardware.
"""

from __future__ import annotations

import ctypes
import gc
import threading
import time
import weakref

from fm350mac import usb_async as ua


def _wait_until(predicate, timeout=1.0, interval=0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeLibusb:
    """Records alloc/submit/cancel/free calls; never talks to real USB.
    ``fire(transfer, status, actual_length=0, data=b"")`` simulates libusb
    completing a transfer and invoking its callback, the way the real
    event-handling loop would.
    """

    def __init__(self) -> None:
        self.submitted: list = []
        self.freed: list = []
        self.cancelled: list = []
        self.clear_halt_calls: list[tuple] = []
        self.submit_transfer_return = 0
        self.close_calls: list = []
        self.exit_calls: list = []
        self.open_raises: Exception | None = None
        # Tracks transfer addresses currently "submitted, not yet retired",
        # to catch a real double-submit of the same transfer (see
        # test_submit_out_never_double_submits_under_concurrent_contention).
        self._pending: set[int] = set()
        self.double_submits: list = []

    def alloc_transfer(self, iso_packets: int = 0):
        return ctypes.pointer(ua.LibusbTransfer())

    def free_transfer(self, transfer) -> None:
        self.freed.append(transfer)

    def submit_transfer(self, transfer) -> int:
        addr = ctypes.addressof(transfer.contents)
        if addr in self._pending:
            self.double_submits.append(transfer)
        else:
            self._pending.add(addr)
        self.submitted.append(transfer)
        return self.submit_transfer_return

    def cancel_transfer(self, transfer) -> int:
        self.cancelled.append(transfer)
        return 0

    def error_name(self, code: int) -> str:
        return f"FAKE_ERROR({code})"

    def clear_halt(self, handle, endpoint) -> int:
        self.clear_halt_calls.append((handle, endpoint))
        return 0

    def handle_events_timeout_completed(self, ctx, timeout_s: float) -> int:
        return 0  # this fake never delivers events on its own; tests fire callbacks directly

    def retire(self, transfer) -> None:
        """Mark ``transfer`` no longer pending (for double-submit tracking).
        Not part of the real Libusb API; only used by tests that reuse a
        slot across multiple submissions.
        """
        self._pending.discard(ctypes.addressof(transfer.contents))

    # --- UsbDevice._open()/close() support ----------------------------------

    def init_context(self):
        return "fake-ctx"

    def exit(self, ctx) -> None:
        self.exit_calls.append(ctx)

    def get_device_list(self, ctx):
        return (["fake-dev"], 1)

    def free_device_list(self, list_pp, unref_devices: int = 1) -> None:
        pass

    def get_device_descriptor(self, dev):
        desc = ua.LibusbDeviceDescriptor()
        desc.idVendor = ua.VID
        desc.idProduct = ua.PIDS[0]
        return desc

    def open(self, dev):
        if self.open_raises is not None:
            raise self.open_raises
        return "fake-handle"

    def close(self, handle) -> None:
        self.close_calls.append(handle)

    @staticmethod
    def fire(transfer, status: int, actual_length: int = 0, data: bytes = b"") -> None:
        if data:
            ctypes.memmove(transfer.contents.buffer, data, len(data))
        transfer.contents.status = status
        transfer.contents.actual_length = actual_length
        transfer.contents.callback(transfer)


# --- pool state machine ------------------------------------------------


def test_completed_in_transfer_delivers_data_and_resubmits():
    lib = FakeLibusb()
    received: list[bytes] = []
    pool = ua.AsyncEndpoint(lib, None, 0x81, "in", "bulk", count=2, buffer_size=8, timeout_ms=0, on_complete=received.append)
    pool.start()
    assert len(lib.submitted) == 2

    lib.fire(lib.submitted[0], ua.LIBUSB_TRANSFER_COMPLETED, actual_length=5, data=b"hello")

    assert received == [b"hello"]
    assert len(lib.submitted) == 3  # resubmitted
    assert not pool.all_retired()


def test_completed_in_transfer_clamps_a_bogus_oversized_actual_length():
    """actual_length is a device/driver-controlled value; a buggy or hostile
    report of more bytes than the buffer can hold must be clamped, not read
    out of bounds (see usb_async._clamped_bytes()).
    """
    lib = FakeLibusb()
    received: list[bytes] = []
    pool = ua.AsyncEndpoint(lib, None, 0x81, "in", "bulk", count=1, buffer_size=8, timeout_ms=0, on_complete=received.append)
    pool.start()

    lib.fire(lib.submitted[0], ua.LIBUSB_TRANSFER_COMPLETED, actual_length=999999, data=b"01234567")

    assert received == [b"01234567"]  # clamped to buffer_size, not 999999
    assert len(received[0]) == 8


def test_timed_out_out_transfer_counts_a_stall_and_frees_the_slot():
    lib = FakeLibusb()
    results: list[tuple[bool, int, object]] = []
    pool = ua.AsyncEndpoint(
        lib, None, 0x01, "out", "bulk", count=1, buffer_size=8, timeout_ms=500,
        on_out_result=lambda ok, actual, meta: results.append((ok, actual, meta)),
    )
    assert pool.submit_out(b"abc", metadata=3)
    assert len(lib.submitted) == 1
    assert not pool.submit_out(b"xyz")  # no free slot: the only one is in flight

    lib.fire(lib.submitted[0], ua.LIBUSB_TRANSFER_TIMED_OUT, actual_length=0)

    assert results == [(False, 0, 3)]
    assert pool.all_retired()  # slot freed, nothing resubmitted for OUT
    assert pool.submit_out(b"again", metadata=1)  # slot is free again


def test_completed_out_transfer_reports_success():
    lib = FakeLibusb()
    results: list[tuple[bool, int, object]] = []
    pool = ua.AsyncEndpoint(
        lib, None, 0x01, "out", "bulk", count=1, buffer_size=8, timeout_ms=500,
        on_out_result=lambda ok, actual, meta: results.append((ok, actual, meta)),
    )
    pool.submit_out(b"abcd", metadata=4)
    lib.fire(lib.submitted[0], ua.LIBUSB_TRANSFER_COMPLETED, actual_length=4)
    assert results == [(True, 4, 4)]
    assert pool.all_retired()


def test_no_device_marks_pool_failed_and_reports_device_lost():
    lib = FakeLibusb()
    fatal_calls: list[tuple[str, bool]] = []
    pool = ua.AsyncEndpoint(
        lib, None, 0x81, "in", "bulk", count=2, buffer_size=8, timeout_ms=0,
        on_complete=lambda data: None, on_fatal=lambda reason, device_lost: fatal_calls.append((reason, device_lost)),
    )
    pool.start()

    lib.fire(lib.submitted[0], ua.LIBUSB_TRANSFER_NO_DEVICE)

    assert pool.failed
    assert len(fatal_calls) == 1
    assert fatal_calls[0][1] is True  # device_lost


def test_stall_schedules_clear_halt_off_the_event_thread_then_resubmits():
    lib = FakeLibusb()
    pool = ua.AsyncEndpoint(lib, None, 0x81, "in", "bulk", count=1, buffer_size=8, timeout_ms=0, on_complete=lambda d: None)
    pool.start()
    caller_thread = threading.current_thread()
    handled_on = {}

    original_clear_halt = lib.clear_halt

    def recording_clear_halt(handle, endpoint):
        handled_on["thread"] = threading.current_thread()
        return original_clear_halt(handle, endpoint)

    lib.clear_halt = recording_clear_halt

    lib.fire(lib.submitted[0], ua.LIBUSB_TRANSFER_STALL)

    # clear_halt runs on a background thread, not synchronously inside the
    # callback (which runs on what stands in for the libusb event thread).
    assert _wait_until(lambda: lib.clear_halt_calls), "clear_halt was never called"
    assert handled_on["thread"] is not caller_thread
    assert _wait_until(lambda: len(lib.submitted) == 2), "transfer was not resubmitted after the stall"


def test_error_status_counts_and_eventually_goes_fatal():
    lib = FakeLibusb()
    fatal_calls: list[str] = []
    pool = ua.AsyncEndpoint(
        lib, None, 0x81, "in", "bulk", count=1, buffer_size=8, timeout_ms=0,
        on_complete=lambda d: None, on_fatal=lambda reason, device_lost: fatal_calls.append(reason),
    )
    pool.start()

    for _ in range(ua._MAX_CONSECUTIVE_ERRORS):
        transfer = lib.submitted[-1]
        submitted_before = len(lib.submitted)
        lib.fire(transfer, ua.LIBUSB_TRANSFER_ERROR)
        if pool.failed:
            break
        assert _wait_until(lambda before=submitted_before: len(lib.submitted) > before or pool.failed, timeout=1.0)

    assert pool.failed
    assert fatal_calls


def test_callback_exception_never_propagates_and_marks_pool_failed():
    lib = FakeLibusb()

    def exploding_on_complete(data):
        raise RuntimeError("boom")

    pool = ua.AsyncEndpoint(lib, None, 0x81, "in", "bulk", count=1, buffer_size=8, timeout_ms=0, on_complete=exploding_on_complete)
    pool.start()

    lib.fire(lib.submitted[0], ua.LIBUSB_TRANSFER_COMPLETED, actual_length=0)  # must not raise

    assert pool.failed


def test_cancel_all_then_all_retired_once_every_transfer_reports_cancelled():
    lib = FakeLibusb()
    pool = ua.AsyncEndpoint(lib, None, 0x81, "in", "bulk", count=3, buffer_size=8, timeout_ms=0, on_complete=lambda d: None)
    pool.start()
    pool.cancel_all()
    assert len(lib.cancelled) == 3
    assert not pool.all_retired()

    for transfer in list(lib.submitted):
        lib.fire(transfer, ua.LIBUSB_TRANSFER_CANCELLED)

    assert pool.all_retired()
    pool.free_all()
    assert len(lib.freed) == 3


# --- EventLoop: nothing is freed before every pool reports fully retired ---


def test_event_loop_stop_does_not_free_anything_if_a_transfer_never_retires():
    lib = FakeLibusb()
    pool = ua.AsyncEndpoint(lib, None, 0x81, "in", "bulk", count=2, buffer_size=8, timeout_ms=0, on_complete=lambda d: None)
    loop = ua.EventLoop(lib, ctx=None, poll_interval_s=0.01)
    loop.register(pool)
    loop.start()
    pool.start()
    assert _wait_until(lambda: len(lib.submitted) == 2)

    # Never fire any callback: the fake's handle_events_timeout_completed()
    # doesn't deliver completions on its own, so nothing ever retires.
    drained = loop.stop(drain_timeout_s=0.2)

    assert drained is False
    assert lib.freed == []  # leaked, not freed -- see EventLoop.stop()


def test_event_loop_stop_frees_everything_once_all_transfers_retire():
    lib = FakeLibusb()
    pool = ua.AsyncEndpoint(lib, None, 0x81, "in", "bulk", count=2, buffer_size=8, timeout_ms=0, on_complete=lambda d: None)
    loop = ua.EventLoop(lib, ctx=None, poll_interval_s=0.01)
    loop.register(pool)
    loop.start()
    pool.start()
    assert _wait_until(lambda: len(lib.submitted) == 2)

    def cancel_and_retire_in_background():
        assert _wait_until(lambda: pool._stopping)
        for transfer in list(lib.submitted):
            lib.fire(transfer, ua.LIBUSB_TRANSFER_CANCELLED)

    t = threading.Thread(target=cancel_and_retire_in_background, daemon=True)
    t.start()
    drained = loop.stop(drain_timeout_s=2.0)
    t.join(timeout=1.0)

    assert drained is True
    assert len(lib.freed) == 2


def test_event_loop_stop_marks_unsafe_and_leaks_when_it_gives_up():
    lib = FakeLibusb()
    dev = ua.UsbDevice(lib, ctx="ctx", handle="handle", pid=ua.PIDS[0])
    pool = ua.AsyncEndpoint(lib, None, 0x81, "in", "bulk", count=2, buffer_size=8, timeout_ms=0, on_complete=lambda d: None)
    loop = ua.EventLoop(lib, ctx="ctx", usb_device=dev, poll_interval_s=0.01)
    loop.register(pool)
    loop.start()
    pool.start()
    assert _wait_until(lambda: len(lib.submitted) == 2)

    drained = loop.stop(drain_timeout_s=0.2)  # nothing ever retires -> gives up

    assert drained is False
    assert dev.unsafe_to_close


def test_event_loop_stop_returns_false_if_background_thread_never_exits():
    lib = FakeLibusb()
    pool = ua.AsyncEndpoint(lib, None, 0x81, "in", "bulk", count=1, buffer_size=8, timeout_ms=0, on_complete=lambda d: None)
    dev = ua.UsbDevice(lib, ctx="ctx", handle="handle", pid=ua.PIDS[0])
    loop = ua.EventLoop(lib, ctx="ctx", usb_device=dev, poll_interval_s=0.01)
    loop.register(pool)

    stuck = threading.Event()

    def never_ending_handle_events(ctx, timeout_s):
        stuck.wait()  # blocks forever (until the test releases it) -- simulates a stuck event thread
        return 0

    lib.handle_events_timeout_completed = never_ending_handle_events
    loop.start()

    drained = loop.stop(drain_timeout_s=0.2)

    assert drained is False
    assert dev.unsafe_to_close
    stuck.set()  # let the background thread exit so it doesn't outlive the test
    loop._thread.join(timeout=1.0)


# --- item 1/2: memory safety once EventLoop.stop() gives up -----------------


def test_close_is_a_permanent_noop_once_marked_unsafe():
    lib = FakeLibusb()
    dev = ua.UsbDevice(lib, ctx="ctx", handle="handle", pid=ua.PIDS[0])

    dev.mark_unsafe_to_close("simulated leaked transfer")
    dev.close()

    assert lib.close_calls == []
    assert lib.exit_calls == []
    assert dev.closed is False  # never actually closed; close() always refuses from now on


def test_leaked_event_loop_keeps_its_callback_reachable_via_weakref():
    lib = FakeLibusb()
    pool = ua.AsyncEndpoint(lib, None, 0x81, "in", "bulk", count=2, buffer_size=8, timeout_ms=0, on_complete=lambda d: None)
    loop = ua.EventLoop(lib, ctx=None, poll_interval_s=0.01)
    loop.register(pool)
    loop.start()
    pool.start()
    assert _wait_until(lambda: len(lib.submitted) == 2)

    callback_ref = weakref.ref(pool._callback)
    drained = loop.stop(drain_timeout_s=0.2)  # nothing retires -> gives up and leaks
    assert drained is False

    del loop, pool
    gc.collect()

    assert callback_ref() is not None, "the leaked CFUNCTYPE callback was garbage collected"


# --- item 4: check-then-submit race at stop ---------------------------------


def test_delayed_resubmit_after_error_never_submits_once_stopping():
    lib = FakeLibusb()
    pool = ua.AsyncEndpoint(lib, None, 0x81, "in", "bulk", count=1, buffer_size=8, timeout_ms=0, on_complete=lambda d: None)
    pool.start()
    slot = pool._slots[0]

    pool.cancel_all()  # sets _stopping under the lock before the delayed resubmit runs
    submitted_before = len(lib.submitted)
    pool._resubmit_after(slot, delay=0)  # what an ERROR/OVERFLOW completion schedules

    assert len(lib.submitted) == submitted_before  # never actually submitted
    assert slot.retired is True
    assert slot.in_flight is False


def test_stop_racing_a_delayed_resubmit_never_leaves_a_transfer_in_flight():
    for _ in range(20):
        lib = FakeLibusb()
        pool = ua.AsyncEndpoint(lib, None, 0x81, "in", "bulk", count=1, buffer_size=8, timeout_ms=0, on_complete=lambda d: None)
        pool.start()
        slot = pool._slots[0]

        t = threading.Thread(target=pool._resubmit_after, args=(slot, 0.001))
        t.start()
        pool.cancel_all()
        t.join(timeout=1.0)

        assert slot.in_flight is False
        assert pool.all_retired()


# --- item 5: submit_out() from two threads never double-submits ------------


def test_submit_out_never_double_submits_under_concurrent_contention():
    for _ in range(20):
        lib = FakeLibusb()
        slot_count = 2
        pool = ua.AsyncEndpoint(
            lib, None, 0x01, "out", "bulk", count=slot_count, buffer_size=8, timeout_ms=500,
            on_out_result=lambda ok, actual, meta: None,
        )
        n_threads = 16
        barrier = threading.Barrier(n_threads)
        results: list[bool] = []
        results_lock = threading.Lock()

        def worker(i):
            barrier.wait()
            ok = pool.submit_out(bytes([i % 256]), metadata=i)
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        assert not lib.double_submits, f"double submit detected: {lib.double_submits}"
        assert sum(results) == slot_count


# --- item 6: sync reads clamp a bogus/oversized transferred length ---------


class _OverreportingLibusb(FakeLibusb):
    """Reports transferring more bytes than the buffer it was given -- a
    buggy or malicious device/driver should never cause an out-of-bounds
    read (see usb_async.UsbDevice.bulk_in/control_in/interrupt_in).
    """

    def bulk_transfer(self, handle, endpoint, data_ptr, length, timeout_ms):
        return 0, length + 10_000

    def interrupt_transfer(self, handle, endpoint, data_ptr, length, timeout_ms):
        return 0, length + 10_000

    def control_transfer(self, handle, request_type, request, value, index, data_ptr, length, timeout_ms):
        return length + 10_000  # libusb_control_transfer returns the transferred count directly


def test_bulk_in_clamps_a_bogus_oversized_transferred_length():
    lib = _OverreportingLibusb()
    dev = ua.UsbDevice(lib, ctx="ctx", handle="handle", pid=ua.PIDS[0])
    data = dev.bulk_in(0x81, length=16)
    assert len(data) == 16


def test_interrupt_in_clamps_a_bogus_oversized_transferred_length():
    lib = _OverreportingLibusb()
    dev = ua.UsbDevice(lib, ctx="ctx", handle="handle", pid=ua.PIDS[0])
    data = dev.interrupt_in(0x82, length=8)
    assert len(data) == 8


def test_control_in_clamps_a_bogus_oversized_transferred_length():
    lib = _OverreportingLibusb()
    dev = ua.UsbDevice(lib, ctx="ctx", handle="handle", pid=ua.PIDS[0])
    data = dev.control_in(0xA1, 0x01, 0, 0, length=4)
    assert len(data) == 4


# --- item 9: UsbDevice._open() never leaks the libusb context on failure ---


def test_open_exits_the_context_exactly_once_if_open_raises():
    lib = FakeLibusb()
    lib.open_raises = ua.UsbError(-1, "LIBUSB_ERROR_IO", "simulated open() failure")

    try:
        ua.UsbDevice._open(ua.VID, ua.PIDS, lib)
        raised = False
    except ua.UsbError:
        raised = True

    assert raised
    assert lib.exit_calls == ["fake-ctx"]


def test_open_exits_the_context_once_when_the_device_is_not_found():
    lib = FakeLibusb()

    def no_matching_device(ctx):
        return ([], 0)

    lib.get_device_list = no_matching_device

    try:
        ua.UsbDevice._open(ua.VID, ua.PIDS, lib)
        raised = False
    except ua.UsbNoDevice:
        raised = True

    assert raised
    assert lib.exit_calls == ["fake-ctx"]
