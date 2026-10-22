"""HTTPdis service lifecycle, independent of Atraxis."""
import signal
import threading

def run_context(context, handler):
    previous = {}
    shutdown = threading.Event()
    cleanup_errors = []

    def stop():
        shutdown.wait()
        try:
            context.stop()
        except Exception:
            cleanup_errors.append(True)

    # Signal handlers only request shutdown; they must not re-enter a startup
    # lock or join a worker interrupted halfway through Thread.start().
    watcher = threading.Thread(target=stop, name='atraxis-shutdown')
    watcher.start()
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda signum, frame: shutdown.set())
        context.run(http_req_handler=handler)
    finally:
        shutdown.set()
        watcher.join()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if cleanup_errors:
        raise ValueError('server_shutdown_failed')
