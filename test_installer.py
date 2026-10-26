"""Exercise the installer's actual credential prompts through a Linux PTY."""
import errno
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
import time
import unittest

try:
    import fcntl
    import pty
    import termios
except ImportError:
    pty = None


@unittest.skipIf(pty is None, "Requires a POSIX terminal")
class InstallerTests(unittest.TestCase):
    def test_credentials_with_script_on_stdin_and_nonseekable_tty(self):
        source = Path(__file__).with_name("install.sh").read_text()
        script = source.split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory() as folder:
            env_path = Path(folder) / "credentials.env"
            script = script.replace("'/etc/ripcars-post.env'", repr(str(env_path)))
            master, slave = pty.openpty()

            def controlling_terminal():
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

            process = subprocess.Popen([sys.executable, "-"], stdin=subprocess.PIPE,
                stdout=slave, stderr=slave, preexec_fn=controlling_terminal)
            os.close(slave)
            transcript = b""
            steps = iter([
                (b"Discord bot token (hidden):", b"fixture-discord-token"),
                (b"TwitterAPI.io API key (hidden):", b"fixture-api-key"),
                (b"Discord server ID:", b"123456789012345678"),
            ])
            pending = next(steps)
            try:
                process.stdin.write(script.encode())
                process.stdin.close()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if not select.select([master], [], [], 0.1)[0]:
                        if process.poll() is not None:
                            break
                        continue
                    try:
                        chunk = os.read(master, 8192)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    transcript += chunk
                    if pending and pending[0] in transcript:
                        os.write(master, pending[1] + b"\n")
                        pending = next(steps, None)
                self.assertEqual(process.wait(timeout=2), 0, transcript.decode())
                self.assertIsNone(pending, "Not all prompts were reached")
                saved = env_path.read_text()
                self.assertIn('DISCORD_BOT_TOKEN="fixture-discord-token"', saved)
                self.assertIn('TWITTERAPI_KEY="fixture-api-key"', saved)
                self.assertIn('DISCORD_GUILD_ID="123456789012345678"', saved)
                self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
                self.assertNotIn(b"fixture-discord-token", transcript)
                self.assertNotIn(b"fixture-api-key", transcript)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                os.close(master)


if __name__ == "__main__":
    unittest.main()
