"""Standard-library compatibility shims across the supported Python range.

ShugoCore supports Python **3.9 through 3.13**: ``requires-python = ">=3.9"``,
and the CI matrix runs the whole suite on every one of those interpreters. A
few stdlib features the codebase genuinely wants are newer than that floor, so
they are folded into this one small, documented module rather than being
sprinkled as ad-hoc ``sys.version_info`` checks through the packages.

Currently:

* :data:`dataclass_slots` -- :func:`dataclasses.dataclass` with ``slots=True``.

  ``slots=True`` was added in Python 3.10. On 3.9 it is not merely ignored: it
  raises ``TypeError: dataclass() got an unexpected keyword argument 'slots'``
  at *class definition* time, i.e. at import time. That single construct took
  out the 3.9 CI leg for three weeks (189 cascading ``TypeError``s, 120 errors)
  because ``personality/governor.py`` and ``kv_mesh/shard.py`` are imported (and
  bundled into the Android APK) transitively by the agent.

  The generated ``__slots__`` is a real memory win for the many small value
  objects in the on-device runtime, so it is *kept* on 3.10+ and only omitted
  on 3.9 -- the classes keep identical field semantics either way.

Usage::

    from py_compat import dataclass_slots

    @dataclass_slots
    class ShardSpec:
        ...

    @dataclass_slots(frozen=True)
    class Frozen:
        ...

Dropping the 3.9 floor is tracked separately; if that lands, this module
collapses back to a plain ``from dataclasses import dataclass``.
"""

import sys
from dataclasses import dataclass
from functools import partial

__all__ = ["HAS_DATACLASS_SLOTS", "dataclass_slots"]

#: True when :func:`dataclasses.dataclass` accepts ``slots=`` (Python 3.10+).
HAS_DATACLASS_SLOTS = sys.version_info >= (3, 10)

if HAS_DATACLASS_SLOTS:
    # ``partial`` keeps both spellings working: bare (``@dataclass_slots``)
    # and parameterised (``@dataclass_slots(frozen=True)``) -- ``dataclass``
    # itself accepts either a class or only keyword arguments.
    dataclass_slots = partial(dataclass, slots=True)
else:  # pragma: no cover - covered by the 3.9 leg of the CI matrix
    # Python 3.9: no ``slots`` support, so fall back to a plain dataclass.
    # Field order/defaults/equality/repr are unchanged; only the
    # ``__slots__`` optimisation is unavailable.
    dataclass_slots = dataclass
