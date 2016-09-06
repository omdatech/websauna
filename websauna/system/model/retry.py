"""Transaction retry point support for command line applications and daemons.."""

import logging
import threading
from functools import wraps

import transaction


logger = logging.getLogger(__name__)


class NotRetryable(Exception):
    """Transaction retry mechanism not configured."""


class TransactionAlreadyInProcess(Exception):
    """ensure_transactionless() detected a dangling transactions."""


class CannotRetryAnymore(Exception):
    """We have reached the limit of transaction retry counts in @retryable."""


def ensure_transactionless(msg=None, transaction_manager=transaction.manager):
    """Make sure the current thread doesn't already have db transaction in process.

    :param transaction_manager: TransactionManager to check. Defaults to thread local transaction manager.
    """

    txn = transaction_manager._txn
    if txn:
        if not msg:
            msg = "Dangling transction open in transaction.manager. You should not start new one."

        transaction_thread = getattr(transaction.manager, "begin_thread", None)
        logger.fatal("Transaction state management error. Trying to start TX in thread %s. TX started in thread %s", threading.current_thread(), transaction_thread)

        raise TransactionAlreadyInProcess(msg)


def is_retryable(txn, error):
    # Emulate TransactionManager.is_retryable
    for dm in txn._resources:
        should_retry = getattr(dm, 'should_retry', None)
        if (should_retry is not None) and should_retry(error):
            return True


def retryable(tm=None, get_tm=None):
    """Function decorator for§ SQL Serialized transaction conflict resolution through retries.

    You need to give either ``tm`` or ``get_tm`` argument.

    * New transaction is started when entering the decorated function

    * If there is already a transaction in progress when entering the decorated function raise an error

    * Commit when existing the decorated function

    * If the commit fails due to a SQL serialization conflict then try to rerun the decorated function max ``tm.retry_attempt_count`` times. Usually this is configured in TODO.

    Example:

    .. code-block:: python

        from websauna.system.model.retry import retryable

        def deposit_eth(web3: Web3, dbsession: Session, opid: UUID):

            @retryable(tm=dbsession.transaction_manager)
            def perform_tx():
                op = dbsession.query(CryptoOperation).get(opid)
                op.mark_performed()
                op.mark_broadcasted()
                # Transaction confirmation count updater will make sure we have enough blocks,
                # and then will call mark_completed()

            perform_tx()

    Example using class based transaction manager resolver:

    .. code-block:: python

        from websauna.system.model.retry import retryable

        class OperationQueueManager:

            def __init__(self, web3: Web3, dbsession: Session, asset_network_id, registry: Registry):
                assert isinstance(registry, Registry)
                assert isinstance(asset_network_id, UUID)
                self.web3 = web3
                self.dbsession = dbsession
                self.asset_network_id = asset_network_id
                self.registry = registry
                self.tm = self.dbsession.transaction_manager

            def _get_tm(*args, **kargs):
                self = args[0]
                return self.tm

            @retryable(get_tm=_get_tm)
            def get_waiting_operation_ids(self) -> List[Tuple[UUID, CryptoOperationType]]:
                wait_list = self.dbsession.query(CryptoOperation, CryptoOperation.id, CryptoOperation.state, CryptoOperation.operation_type).filter_by(network_id=self.asset_network_id, state=CryptoOperationState.waiting)

                # Flatten
                wait_list = [(o.id, o.operation_type) for o in wait_list]
                return wait_list

            def run_waiting_operations(self):
                # Performed inside TX retry boundary
                ops = self.get_waiting_operation_ids()

    :param tm: Transaction manager used to control the TX execution

    :param get_tm: Factory function that is called with *args and **kwargs to get the transaction manager
    """

    def _transaction_retry_wrapper(func):

        @wraps(func)
        def decorated_func(*args, **kwargs):

            global _retry_count

            if get_tm:
                manager = get_tm(*args, **kwargs)
            else:
                # Get how many attempts we want to do
                manager = tm

            assert manager, "No transaction manager available for retry"

            # Make sure we don't re-enter to transaction
            ensure_transactionless(transaction_manager=manager)

            retry_attempt_count = getattr(manager, "retry_attempt_count", None)
            if not retry_attempt_count:
                raise NotRetryable("TransactionManager is not configured with default retry attempt count")

            # Run attempt loop
            latest_exc = None
            for num in range(retry_attempt_count):
                if num >= 1:
                    logger.info("Transaction attempt #%d for function %s", num + 1, func)

                txn = manager.begin()

                # Expose retry count for testing
                manager.latest_retry_count = num

                try:
                    val = func(*args, **kwargs)
                    txn.commit()
                    return val
                except Exception as e:
                    if is_retryable(txn, e):
                        latest_exc = e
                        continue
                    else:
                        raise e

            raise CannotRetryAnymore("Out of transaction retry attempts, tried {} times".format(num + 1)) from latest_exc

        return decorated_func

    return _transaction_retry_wrapper