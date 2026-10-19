# lights-out

**Claude Code works the night shift on your laptop. One agent at a time. It waits out the usage limit, never starts while you're busy, and leaves you a report for the morning.**

![A night replayed: a job waits while you're on a call, a temporary error gets retried, the usage limit pauses everything until the reset, and the morning report says what happened](docs/demo.gif)

<sub>The recording replays one night through the real CLI with a stand-in for `claude`, so it doesn't burn real usage. Script: [docs/demo/setup.sh](docs/demo/setup.sh).</sub>

```bash
curl -fsSL https://raw.githubusercontent.com/PepSecanell/lights-out/main/install.sh | sh
```

## Why I built this

I run a lot of Claude Code jobs on my MacBook while I sleep. Video edits, scripts, posting kits, about ten of them a night. For two months they broke in every way you can imagine, and almost none of it was the agent's fault.

- The login expired at 23:00 and every job failed until morning.
- The usage limit hit at midnight and every job got marked "failed", so the retry counters ran out on work that was totally fine.
- One `API Error: 500` killed a 40 minute edit. A retry would have saved it.
- Two jobs edited the same folder at once. One deleted the other's finished work.
- A job started while I was recording a video and the fans spun up on camera.
- launchd quietly skipped every weekly job, because a calendar entry with a weekday doesn't catch up after the Mac was asleep. It just waits another week. Seven jobs had zero runs and nobody told me.
- launchd can't open a log file inside `~/Downloads`. The agent dies before it writes a single line (exit 78).
- macOS started writing a narrow no-break space before "PM" in a log, and my "am I recording?" check silently stopped matching.

Every fix ended up in a small shared runner. This repo is that runner, cleaned up so it works for anyone's jobs, not just mine.

## What it does

**One agent at a time.** Jobs share one slot. Your laptop has one CPU budget and your plan has one usage limit, so they take turns, best priority first. Locks have a pid and a heartbeat, so a crashed job never blocks the rest.

**It knows why a run failed.** Every failed `claude -p` run is sorted into a kind, and each kind gets its own answer.

| kind | example | what lights-out does |
|---|---|---|
| `usage_limit` | `You've hit your session limit · resets 1am` | Reads the reset time, pauses *every* job until then, carries on after. Not a failure. |
| `transient` | `API Error: 500`, overloaded, network | Retries in the same run (30 s, 90 s, 240 s). If the session already got somewhere it resumes it with `--resume` instead of starting over. |
| `auth_expired` | `OAuth session expired` | Stops trying for an hour and tells you to log in. Not a failure. |
| `failure` | the tests still fail | The only kind that counts. Three in a row and the job is parked until you `lightsout reset` it. |

**It never starts while you're busy.** A job only starts if none of your busy apps are running (Zoom, OBS, Keynote, whatever you list), your own check says go, you haven't touched the keyboard for N minutes, you're on power, and the disk has room. A job that's already running is left alone. Killing an agent halfway through an edit is worse.

**It stays inside the night.** Jobs only start inside your window (say 22:00 to 06:00). A run that was missed because the laptop was asleep still happens later that night.

**It keeps a budget.** `allowed_models = ["sonnet", "haiku"]` turns "oops, that ran on the big model all night" into a config error. `nightly_budget_usd` stops new jobs once tonight's reported cost passes your cap, and `max_budget_usd` caps a single job.

**It doesn't touch your checkout.** With `worktree = true` the agent works in a fresh git worktree on its own branch. Whatever it leaves gets committed there, and the report names the branch. You merge what you like.

**It leaves a morning report.** One plain-English page and one notification. What finished and what it said, what failed and why, what waited for the limit, what's parked, and which branch to look at.

```
Night report, morning of Mon 19 Oct 2026

│ Usage limit reached at 23:05. Every job paused until 01:03, then carried on.

Done
- triage-issues 23:05 ($0.42, waited 1x): Labelled 7 new issues, 2 need you
- fix-failing-tests 23:05 ($0.54, 1 retry, branch lightsout/fix-failing-tests/20261018-2305): Fixed the flaky date parser test
- update-docs 01:10 ($0.42): Updated 2 doc pages to match the new API

Failed
- outdated-deps 01:10: npm ERR! code ENOENT: package.json not found
```

## Quick start (about 5 minutes)

You need [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and logged in, and Python 3.9 or newer (3.11+ for TOML configs, or `pip install tomli`).

```bash
curl -fsSL https://raw.githubusercontent.com/PepSecanell/lights-out/main/install.sh | sh
```

or, if you'd rather not pipe to a shell:

```bash
uv tool install git+https://github.com/PepSecanell/lights-out   # or: pipx install git+https://...
lightsout init
```

`init` writes `~/.config/lightsout/lightsout.toml` with one safe job called `hello` that only looks at a new empty folder, `~/lights-out-playground`. Try it right now:

```bash
lightsout check        # what will run and when
lightsout run hello    # run it now, through the same slot, limit and guards as the night
lightsout logs hello   # what the agent did, readable
```

Then let it run every night:

```bash
lightsout install      # launchd on macOS, a systemd user timer on Linux, cron anywhere else
lightsout status
```

A sleeping Mac runs nothing. `install` prints the one `pmset` line that wakes it for your window, if you want that.

## A job

```toml
[night]
window = "22:00-06:00"
allowed_models = ["sonnet", "haiku"]
nightly_budget_usd = 10

[guards]
busy_processes = ["zoom.us", "obs"]
user_idle_minutes = 15
on_battery = "skip"

[[job]]
name = "fix-failing-tests"
schedule = "daily 23:00"            # or "weekly mon,thu 01:30", "every 2h", "manual"
cwd = "~/code/my-app"
worktree = true                      # its own branch, never your checkout
model = "sonnet"
prompt_file = "prompts/fix-failing-tests.md"
allowed_tools = ["Read", "Edit", "Grep", "Bash(npm test:*)"]
permission_mode = "acceptEdits"
max_budget_usd = 3
timeout_minutes = 60
```

A job is a `prompt`, a `prompt_file`, or a plain `command`. Commands get the same slot, guards, error sorting and report, so your non-AI night scripts can live here too. The full list of keys with comments is in the [starter config](src/lightsout/templates/lightsout.toml).

## Commands

| | |
|---|---|
| `lightsout check` | validate the config, list jobs, show what's due |
| `lightsout status` | window, slot, limit, tonight's spend, every job's last result |
| `lightsout run <job>` | run one job now (`--force` skips the busy guards) |
| `lightsout report` | last night's report in the terminal |
| `lightsout logs <job>` | the latest session, readable |
| `lightsout lock <folder>` | keep jobs out of a folder you're working in by hand (3 h, renewable) |
| `lightsout reset <job>` | put a parked job back |
| `lightsout install` / `uninstall` | the scheduler entry |
| `lightsout doctor` | checks the setup |
| `lightsout classify "<error text>"` | which kind is this error? |

## How it works

The scheduler doesn't know about your jobs. It runs one thing, `lightsout tick`, every 10 minutes, all day. Almost every tick does nothing and exits. A tick that lands inside the window, with a job due, nothing blocking it (limit, login, budget) and every guard saying go, takes the slot and runs the due jobs one by one. It checks the guards again before each one.

I went with one tick instead of a scheduler entry per job because of the launchd weekday bug above. A frequent tick plus "is it due, was it missed" logic catches every missed run, and it behaves the same on launchd, systemd and cron.

Everything lives in small JSON files under `~/Library/Application Support/lightsout` (macOS) or `~/.local/state/lightsout` (Linux). Logs go to `~/Library/Logs/lightsout`, never into Downloads, Desktop or Documents. No daemon, no database, no dependencies beyond the Python standard library.

## Safety notes

Read these before you give an agent your laptop for the night.

- **An unattended agent does what its permissions allow.** Start with `permission_mode = "acceptEdits"` and a tight `allowed_tools` list. `bypassPermissions` is refused unless the job also has `worktree = true`.
- **Use `worktree = true` for anything that edits code.** Your branch and your uncommitted work stay untouched.
- **Set `allowed_models` and a budget.** The report shows the cost Claude Code reports for each run.
- **Prompts are code.** Don't point a night job at text other people can write (issues, emails, web pages) with write tools turned on, unless you're fine with them steering your agent.
- **Secrets.** lights-out never reads or stores your Claude login. If you set the optional `token_file` (a `claude setup-token` token), it has to be `chmod 600` or it's ignored, and it's only passed to the `claude` process as an environment variable. It's never logged.
- **The busy guards only stop jobs from starting.** If you sit down at 2 am, the running job finishes first.

## Compared to

There are good tools near this. Here's where lights-out sits.

- [marcus/nightshift](https://github.com/marcus/nightshift) runs a built-in catalog of code upkeep tasks (dead code, doc drift, test gaps) on your leftover budget and opens PRs. Great if that's the job you want. lights-out runs *your* jobs, any prompt or command, and cares about sharing the laptop with you.
- [claude-auto-resume](https://github.com/terryso/claude-auto-resume) and [claude-marathon](https://github.com/styamanda/claude-marathon) keep *one* long task alive across limit resets. lights-out is for many jobs that have to take turns.
- Claude Code's own [scheduled tasks](https://code.claude.com/docs/en/scheduled-tasks) (Desktop, cloud routines, `/loop`) are the easiest way to run one prompt on a timer. lights-out adds the parts you need once there are several unattended jobs on one machine: the slot, the shared limit pause, the busy guards, worktrees and the report.

## Platforms

macOS (launchd) is what I run every night. Linux works with a systemd user timer (`loginctl enable-linger $USER` keeps it running while you're logged out) or cron. Windows isn't supported yet.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests replay whole nights with a stand-in for `claude` and a frozen clock (`LIGHTSOUT_NOW`), so they run in seconds and spend nothing.

## License

MIT. Made by Pep Secanell ([pepsecanell.com](https://pepsecanell.com), [@Pep_Secanell](https://x.com/Pep_Secanell)). If it saves you a night, a star helps other people find it.
