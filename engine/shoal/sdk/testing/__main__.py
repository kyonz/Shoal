"""``python -m shoal.sdk.testing URL --token T``: the remote contract checks (same as ``shoal integrations check``)."""

import sys

from shoal.sdk.testing.contract import main

sys.exit(main())
