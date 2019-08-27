"""Threading utilities."""

import time
import threading

from .timing import TimeoutException


def execute_as_thread(target, args=None, kwargs=None, daemon=False, join=True,
                      break_join=None, join_sleep=0.01, timeout=None):
    """
    Execute target callable in a separate thread.

    :param target: Target callable.
    :type target: ``callable``
    :param args: Callable args.
    :type args: ``tuple``
    :param kwargs: Callable kwargs.
    :type kwargs: ``kwargs``
    :param daemon: Set daemon thread.
    :type daemon: ``bool``
    :param join: Join thread before return.
    :type join: ``bool``
    :param break_join: Condition for join early break.
    :type break_join: ``callable``
    :param join_sleep: Join break condition check sleep time.
    :type join_sleep: ``int``
    :param timeout: Timeout duration.
    :type timeout: :py:class:`~testplan.common.utils.timing.TimeoutException`
    """
    thr = threading.Thread(target=target, args=args or tuple(),
                           kwargs=kwargs or {})
    thr.daemon = daemon
    thr.start()
    if join is True:
        start_time = time.time()
        while True:
            if not thr.is_alive():
                return
            if break_join is not None and break_join():
                break
            if timeout and time.time() - start_time > timeout:
                raise TimeoutException('Thread {} timeout after {}s'.format(
                    thr, timeout))
            time.sleep(join_sleep)


def interruptible_join(thread, timeout=None):
    """
    Joining a thread without ignoring signal interrupts.

    :param thread: Thread object to wait to terminate.
    :type thread: ``threading.Thread``
    :param timeout: If specified, TimeoutException will be raised if the thread
        does not terminate within the specified timeout.
    :type timeout: ``Optional[numbers.Number]``
    """
    if timeout is None:
        end_time = None
    else:
        end_time = time.time() + timeout

    while end_time is None or time.time() < end_time:
        time.sleep(0.1)
        if not thread.is_alive():
            thread.join()
            break

    if thread.is_alive():
        raise TimeoutException(
            'Thread {thr} timed out after {timeout} seconds.'
            .format(thr=thread, timeout=timeout))

