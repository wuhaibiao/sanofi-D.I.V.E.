"""python -m memsys 等同于 memsys 命令。"""

import sys

from .cli import main

sys.exit(main())
