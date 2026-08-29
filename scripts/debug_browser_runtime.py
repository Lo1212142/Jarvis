from openjarvis.browser.computer import BrowserComputerSession, PlaywrightRuntime

runtime = PlaywrightRuntime(user_data_dir='/tmp/openjarvis-debug-browser', headless=True)
session = BrowserComputerSession(runtime=runtime)
try:
    print(session.start())
except Exception as exc:
    print(type(exc).__name__, repr(exc))
    raise
finally:
    session.stop()
