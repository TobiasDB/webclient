"""``python -m eval.lab [port]`` -- serve the lab for a browser / the playground."""

import sys
import time

from . import LabServer

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
srv = LabServer(port=port)
print(f"eval lab at {srv.base}/lab  (Ctrl-C to stop)")
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    srv.close()
