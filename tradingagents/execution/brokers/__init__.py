from .simulated import SimulatedBroker

__all__ = ["SimulatedBroker"]


def __getattr__(name: str):
    """Lazy import for AlpacaBroker so the alpaca-py dependency stays optional."""
    if name == "AlpacaBroker":
        from .alpaca import AlpacaBroker
        return AlpacaBroker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
