import getpass
import io
import json
import os
import stat
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from redh import completion
from redh.cli import main

from fakes import Lab, nextcloud

HERE = os.path.dirname(os.path.abspath(__file__))


class CompletionTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        nextcloud(self.lab)
        self.lab.write("blog/quadlet/blog.container", "[Container]\nImage=docker.io/x/blog:1\n")
        os.makedirs(os.path.join(self.lab.settings.quadlet_dir, "nextcloud"))
        self.cache = self.lab.settings.completion_cache
        patch = mock.patch.object(completion, "load_settings", lambda config=None: self.lab.settings)
        patch.start()
        self.addCleanup(patch.stop)

    def remember(self):
        self.lab.host.euid = 0                 # only root writes the names down
        completion.remember(self.lab.ctx)

    def test_root_remembers_the_names(self):
        completion.remember(self.lab.ctx)
        self.assertFalse(os.path.exists(self.cache))   # not as root: nothing written
        self.remember()
        with open(self.cache) as fh:
            self.assertEqual(json.load(fh), {"apps": ["blog", "nextcloud"],
                                             "installed": ["nextcloud"],
                                             "known": ["blog", "nextcloud"]})
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o644)
        self.assertEqual(os.listdir(os.path.dirname(self.cache)), ["apps.json"])

    def test_a_directory_others_can_change_gets_nothing(self):
        os.makedirs(os.path.dirname(self.cache))
        os.chmod(os.path.dirname(self.cache), 0o777)
        self.remember()
        self.assertFalse(os.path.exists(self.cache))

    def test_names_come_from_the_apps_root_when_it_is_readable(self):
        self.assertEqual(completion.names("installed"), ["nextcloud"])
        self.assertEqual(completion.completer("apps")("b"), ["blog"])
        self.assertEqual(completion.completer("known")(""), ["blog", "nextcloud"])

    @unittest.skipIf(os.geteuid() == 0, "root reads every directory")
    def test_names_come_from_the_cache_when_it_is_not(self):
        self.remember()
        os.chmod(self.lab.settings.root, 0)
        self.addCleanup(os.chmod, self.lab.settings.root, 0o700)
        self.assertEqual(completion.names("known"), ["blog", "nextcloud"])
        os.unlink(self.cache)
        with mock.patch.object(completion, "argcomplete", None):     # no hint on stderr
            self.assertEqual(completion.names("known"), [])


class WrapperTest(unittest.TestCase):
    def test_sudo_and_the_like_are_cut_off(self):
        for line, expected in (("sudo redh check b", "redh check b"),
                               ("sudo -u root /usr/local/bin/redh st", "/usr/local/bin/redh st"),
                               ("REDH_CONFIG=x redh start ", "redh start "),
                               ("redh check redh", "redh check redh"),
                               ("sudo predh x", "sudo predh x")):
            env = {"COMP_LINE": line, "COMP_POINT": str(len(line))}
            completion.strip_wrapper(env)
            self.assertEqual(env["COMP_LINE"], expected)
            self.assertEqual(env["COMP_POINT"], str(len(expected)))


@unittest.skipIf(completion.argcomplete is None, "argcomplete is not installed")
class ShellTest(unittest.TestCase):
    """What the shell sees: redh run the way argcomplete's shell code runs it."""

    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        nextcloud(self.lab)
        self.config = os.path.join(self.lab.tmp, "redh.conf")
        with open(self.config, "w") as fh:
            fh.write(f"[redh]\nuser = {getpass.getuser()}\nroot = {self.lab.settings.root}\n"
                     f"quadlet_dir = {self.lab.settings.quadlet_dir}\n")

    def complete(self, line):
        answer = os.path.join(self.lab.tmp, "answer")
        env = dict(os.environ, _ARGCOMPLETE="1", COMP_LINE=line, COMP_POINT=str(len(line)),
                   _ARGCOMPLETE_STDOUT_FILENAME=answer, _ARGCOMPLETE_IFS="\n",
                   REDH_CONFIG=self.config, PYTHONPATH=os.path.dirname(HERE))
        subprocess.run([sys.executable, "-m", "redh"], env=env, check=False,
                       stderr=subprocess.DEVNULL, timeout=60)
        with open(answer) as fh:
            return sorted(fh.read().split())

    def test_commands_and_apps(self):
        self.assertEqual(self.complete("redh sta"), ["start", "status"])
        self.assertEqual(self.complete("redh check ne"), ["nextcloud"])
        self.assertEqual(self.complete("redh start ne"), [])          # not installed yet
        self.assertIn("--dry-run", self.complete("redh install --dr"))
        self.assertEqual(self.complete("sudo redh check ne"), ["nextcloud"])

    def test_shell_code(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["completion", "zsh"]), 0)
        self.assertIn("redh", out.getvalue())
        self.assertIn("compdef", out.getvalue())


if __name__ == "__main__":
    unittest.main()
