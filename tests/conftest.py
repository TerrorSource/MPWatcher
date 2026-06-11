import os
import sys
import tempfile
from pathlib import Path

# Tests draaien tegen een wegwerp-configmap en zonder schedulerthread,
# zodat er geen netwerkverkeer of achtergrondwerk plaatsvindt.
os.environ["MPWATCHER_CONFIG_DIR"] = tempfile.mkdtemp(prefix="mpwatcher-test-")
os.environ["MPWATCHER_DISABLE_WORKER"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
