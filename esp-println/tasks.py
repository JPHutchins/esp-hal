import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

import camas_shared

camas_shared.setup(__file__, globals())
