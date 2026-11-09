"""CPU-only MLX surface for hermetic daemon tests.

Array operations use NumPy, never a Metal device. MLX integer arrays and arange
use int32 by default; NumPy uses platform-sized integers unless told otherwise.
"""


class _Metal(object):
    @staticmethod
    def device_info():
        return {"max_recommended_working_set_size": 1 << 30}


metal = _Metal()

_wired = [0]


def set_wired_limit(n):
    prev = _wired[0]
    _wired[0] = n
    return prev


def array(values, dtype=None):
    import numpy as np
    result = np.array(values, dtype=dtype)
    if dtype is None and result.dtype.kind in "iu":
        result = result.astype(np.int32)
    return result


def arange(*args, **kwargs):
    import numpy as np
    if "dtype" not in kwargs:
        kwargs["dtype"] = np.int32
    return np.arange(*args, **kwargs)


def stack(values, axis=0):
    import numpy as np
    return np.stack(values, axis=axis)


def zeros(shape, dtype=None):
    import numpy as np
    return np.zeros(shape, dtype=dtype)


def concatenate(values, axis=0):
    import numpy as np
    return np.concatenate(values, axis=axis)


def eval(*arrays):
    pass


def synchronize():
    pass
