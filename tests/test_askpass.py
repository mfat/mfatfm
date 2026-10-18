import os
import subprocess

from mfatfm.askpass import AskpassServer, prompt_kind


def _run_helper(env, prompt, hint=""):
    full_env = {**os.environ, **env}
    if hint:
        full_env["SSH_ASKPASS_PROMPT"] = hint
    return subprocess.run(
        [env["SSH_ASKPASS"], prompt], env=full_env, capture_output=True, text=True, timeout=10
    )


def test_helper_round_trip():
    server = AskpassServer()
    seen = []

    def handler(prompt, kind):
        seen.append((prompt, kind))
        return None if "refuse" in prompt else "s3cret"

    try:
        env = server.register(handler)
        done = _run_helper(env, "user@host's password: ")
        assert done.returncode == 0 and done.stdout == "s3cret\n"
        assert seen == [("user@host's password: ", "secret")]

        refused = _run_helper(env, "please refuse")
        assert refused.returncode == 1 and refused.stdout == ""

        # A request for an unknown connection is refused without asking.
        stray = dict(env, MFATFM_ASKPASS_CONTEXT="nope")
        assert _run_helper(stray, "password").returncode == 1
        forged = dict(env, MFATFM_ASKPASS_TOKEN="0" * 32)
        assert _run_helper(forged, "password").returncode == 1
        assert len(seen) == 2

        server.unregister(env)
        assert _run_helper(env, "password").returncode == 1
    finally:
        server.close()
    assert not os.path.exists(env["SSH_ASKPASS"])


def test_prompt_kind():
    assert prompt_kind("Enter passphrase for key '/x': ") == "secret"
    question = "Are you sure you want to continue connecting (yes/no/[fingerprint])? "
    assert prompt_kind(question) == "confirm"
    assert prompt_kind("Confirm user presence", "confirm") == "confirm"
    assert prompt_kind("Touch your key", "none") == "info"
