import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from integration_runner import IntegrationSSH
ssh = IntegrationSSH(host="10.51.246.22", port=5023,
                     username="EWISBAY", password="4prilMay@2026!#",
                     log_callback=print)
ssh.connect()
try:
    print(ssh.run_command("ls -1 /home/shared/ESETARI/INOC/SCRIPTS/ | grep -i -E 'sgw|swg'", timeout=15))
finally:
    ssh.disconnect()
