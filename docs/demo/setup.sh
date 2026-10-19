# Sourced (hidden) by demo.tape. Replays one night with a stand-in for claude (tests/fake_claude.py),
# so the recording is real lights-out output without spending real usage. `at HH:MM` moves the clock.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
D=/tmp/lights-out-demo; rm -rf "$D"; mkdir -p "$D"
export LIGHTSOUT_HOME="$D/home" LIGHTSOUT_CONFIG="$D/lightsout.toml" LIGHTSOUT_NO_NOTIFY=1 \
       LIGHTSOUT_BACKOFF_SCALE=0.02 FAKE_CLAUDE_SCRIPT="$D/script" FAKE_CLAUDE_DELAY=0.7 FORCE_COLOR=1
mkdir -p "$D/my-app" && git -C "$D/my-app" init -q && echo "# my-app" > "$D/my-app/README.md" \
  && git -C "$D/my-app" add -A && git -C "$D/my-app" -c user.name=demo -c user.email=demo@example.com commit -qm init
touch "$D/on-a-call"
cat > "$LIGHTSOUT_CONFIG" <<CFG
[night]
window = "22:00-06:00"
claude_bin = "$ROOT/tests/fake_claude.py"
nightly_budget_usd = 10
allowed_models = ["sonnet", "haiku"]

[guards]
busy_command = "test -f $D/on-a-call && echo 'you are on a call'"
on_battery = "allow"
wait_for_network_s = 0

[[job]]
name = "triage-issues"
schedule = "daily 22:30"
cwd = "$D/my-app"
model = "haiku"
prompt = "Label new issues"

[[job]]
name = "fix-failing-tests"
schedule = "daily 23:00"
cwd = "$D/my-app"
worktree = true
model = "sonnet"
priority = 1
prompt = "Fix the failing tests"

[[job]]
name = "update-docs"
schedule = "daily 23:00"
cwd = "$D/my-app"
model = "sonnet"
prompt = "Update the docs"

[[job]]
name = "outdated-deps"
schedule = "weekly mon 01:00"
cwd = "$D/my-app"
command = "echo 'npm ERR! code ENOENT: package.json not found' >&2; exit 1"
CFG
cat > "$FAKE_CLAUDE_SCRIPT" <<SCRIPT
transient
edit:tests.txt:Fixed the flaky date parser test
ok:Labelled 7 new issues, 2 need you
limit:1am
ok:Updated 2 doc pages to match the new API
SCRIPT
export PATH="$ROOT/.venv/bin:$PATH"
at() { export LIGHTSOUT_NOW="2026-10-$( [ "${1%%:*}" -ge 12 ] && echo 18 || echo 19 )T$1"; printf '\033[2m── %s ──\033[0m\n' "$1"; }
calls_end() { rm -f "$D/on-a-call"; }
clear
