"""Relation journal: interrupted file resumes without replaying verified files."""
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import integration_runner
import relation_journal

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        failures.append(name)


class FakeRelationSSH:
    def __init__(self):
        self.logs = {}
        self.progress = set()
        self.current_log = ""
        self.executed = []
        self.interrupt_once = "07_relation.txt"
        self.registered = []

    def register_remote_log(self, path, subfolder=None):
        self.registered.append((path, subfolder))

    def run_amos_command_safe(self, command, node, timeout=60, **kwargs):
        if command.startswith("!rm -f") and "&& touch" in command:
            self.progress.clear()
            return ""
        if command.startswith("!cat"):
            return "".join(f"DONE {marker}\n" for marker in self.progress)
        if command.startswith("!printf"):
            match = re.search(r"DONE ([0-9a-f]{16})", command)
            if match:
                self.progress.add(match.group(1))
            return ""
        if command.startswith("!rm -f"):
            match = re.search(r'"([^"]+\.log)"', command)
            if match:
                self.logs.pop(match.group(1), None)
            return ""
        if command.startswith("l+ "):
            self.current_log = command[3:].strip()
            return "logging started"
        if command == "l-":
            self.current_log = ""
            return "logging stopped"
        return ""

    def run_amos_blocking_with_sentinel(self, command, node, **kwargs):
        remote = command[4:].strip()
        name = os.path.basename(remote)
        self.executed.append(name)
        if name == self.interrupt_once:
            self.interrupt_once = ""
            output = f"> run {remote}\nchannel disconnected\n"
        else:
            output = (
                f"> run {remote}\n"
                "Proxy  MO\n1 MOs set\n"
                "__TRFS_DONE_deadbeef__\n"
            )
        if self.current_log:
            self.logs[self.current_log] = output
        return output

    def sftp_download(self, remote, local):
        if remote not in self.logs:
            raise FileNotFoundError(remote)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "w", encoding="utf-8") as handle:
            handle.write(self.logs[remote])
        return local


tmpdir = tempfile.mkdtemp(prefix="relation-journal-test-")
try:
    input_zip = os.path.join(tmpdir, "relation.zip")
    with open(input_zip, "wb") as handle:
        handle.write(b"stable relation package identity")
    remote_dir = "/home/shared/user/RELATION/SITEA"
    files = [
        f"{remote_dir}/01_relation.txt",
        f"{remote_dir}/07_relation.txt",
        f"{remote_dir}/12_relation.txt",
    ]
    fake = FakeRelationSSH()
    messages = []

    print("\n[1] interruption leaves exact file ambiguous")
    ok1, _ = integration_runner._run_relation_files_resumable(
        fake, "NODEA", "SITEA", input_zip, remote_dir, files, tmpdir,
        messages.append, "", wait_for_user=lambda message: False,
        ui_cb=messages.append,
    )
    path = relation_journal.journal_path(tmpdir, "NODEA")
    first = relation_journal.load(path)
    statuses = {item["file"]: item["status"] for item in first["items"]}
    check("first attempt reports interrupted", not ok1)
    check("file 01 durably verified", statuses["01_relation.txt"] == "VERIFIED_OK",
          str(statuses))
    check("file 07 is ambiguous", statuses["07_relation.txt"] == "AMBIGUOUS",
          str(statuses))
    check("file 12 remains pending", statuses["12_relation.txt"] == "PENDING",
          str(statuses))

    print("\n[2] resume skips verified file and continues from ambiguous file")
    ok2, _ = integration_runner._run_relation_files_resumable(
        fake, "NODEA", "SITEA", input_zip, remote_dir, files, tmpdir,
        messages.append, "", wait_for_user=lambda message: True,
        ui_cb=messages.append,
    )
    second = relation_journal.load(path)
    check("resume completes", ok2 and second["status"] == "COMPLETED",
          second["status"])
    check("verified file 01 was not replayed",
          fake.executed.count("01_relation.txt") == 1, str(fake.executed))
    check("ambiguous file 07 alone was retried",
          fake.executed.count("07_relation.txt") == 2, str(fake.executed))
    check("remaining file 12 ran once",
          fake.executed.count("12_relation.txt") == 1, str(fake.executed))
    check("all items verified after resume",
          all(item["status"] == "VERIFIED_OK" for item in second["items"]),
          str([(i["file"], i["status"]) for i in second["items"]]))
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)

if failures:
    print(f"\nFAILED: {failures}")
    sys.exit(1)
print("\nAll relation journal checks passed.")
