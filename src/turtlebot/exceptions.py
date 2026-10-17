"""Exception hierarchy of the trading bot.

Every error raised deliberately by ``turtlebot`` derives from :class:`TurtleBotError`, so callers
can distinguish expected operational failures from programming errors. The second level groups
errors by subsystem, which is what the retry and alerting logic keys on:

.. code-block:: text

    TurtleBotError
    ├── ConfigurationError
    ├── BrokerError
    │   ├── BrokerConnectionError        (transient: retried with backoff)
    │   ├── NotPaperAccountError         (fatal: never retried)
    │   ├── BrokerDataUnavailableError   (transient)
    │   └── OrderRejectedError
    ├── MarketDataError
    │   └── EmptyMarketDataError
    ├── StateError
    │   ├── PersistenceError
    │   └── ReconciliationAbortedError
    ├── RiskLimitError
    └── JobTimeoutError
"""

from __future__ import annotations


class TurtleBotError(Exception):
    """Base class for all errors raised by the bot."""


class ConfigurationError(TurtleBotError):
    """Invalid or inconsistent configuration."""


# --------------------------------------------------------------------------- broker
class BrokerError(TurtleBotError):
    """Any failure while talking to the broker (TWS / IB Gateway)."""


class BrokerConnectionError(BrokerError):
    """The connection to TWS could not be established or was lost.

    This error is considered transient: the engine reconnects with exponential backoff.
    """


class NotPaperAccountError(BrokerError):
    """The connected account is not a paper-trading account.

    This is a hard safety stop and is never retried.
    """


class BrokerDataUnavailableError(BrokerError):
    """The broker did not deliver data that is required for a safe decision."""


class OrderRejectedError(BrokerError):
    """TWS rejected an order placement, modification or cancellation.

    Attributes:
        order_id: Broker order id the rejection refers to, if known.
        code: TWS error code, if known.
    """

    def __init__(
        self, message: str, *, order_id: int | None = None, code: int | None = None
    ) -> None:
        super().__init__(message)
        self.order_id = order_id
        self.code = code


# --------------------------------------------------------------------------- market data
class MarketDataError(TurtleBotError):
    """Market data could not be loaded or is malformed."""


class EmptyMarketDataError(MarketDataError):
    """A data request returned no usable rows."""


# --------------------------------------------------------------------------- state
class StateError(TurtleBotError):
    """Problems with the local source of truth."""


class PersistenceError(StateError):
    """The SQLite state store could not be read or written."""


class ReconciliationAbortedError(StateError):
    """Reconciliation was aborted because the broker snapshot is not trustworthy."""


# --------------------------------------------------------------------------- misc
class RiskLimitError(TurtleBotError):
    """A risk limit prevents the requested action."""


class JobTimeoutError(TurtleBotError, TimeoutError):
    """A blocking job exceeded its watchdog timeout."""
