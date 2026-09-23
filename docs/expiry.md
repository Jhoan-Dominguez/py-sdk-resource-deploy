# Expiry

Resources can carry an **expiry date** (`expires_at`) in the inventory. Nothing is deleted
automatically when the date passes. Expiry only marks resources as due, so you can review
them and remove them with `undeploy --expired`.

## Setting it

```
python3 src/main.py deploy iam --env dev --ttl 7d --apply        # everything this run records
python3 src/main.py inventory import iam --env dev --origin created --ttl 2w --apply
python3 src/main.py inventory expire temp-tool-dev --in 3d        # one resource (or several)
python3 src/main.py inventory expire 20260922T101500Z-1a2b3c4d --at 2026-10-01
python3 src/main.py inventory expire temp-tool-dev --clear
```

- **Formats:** a duration from now (`12h`, `7d`, `2w`) or a UTC date/time (`2026-10-01`,
  `2026-10-01T18:00`, `2026-10-01T18:00:00Z`). A date without a time means 00:00 UTC.
- **`--ttl` on deploy/import:** every resource the run records gets the same expiry. A
  later deploy **without** `--ttl` leaves an existing expiry untouched. A deploy with
  `--ttl` overwrites it.
- **`inventory expire`:** takes the same targets as `keep` (resource name, ARN, or the
  deployment id that created it), narrowed with `--env` / `--service`. Like `keep`, it
  only changes the inventory and applies immediately. Note that a name can match more
  than one entry (e.g. a role and a policy both named `keycloak-admin-exec-dev`); all of
  them are changed.
- Undeploying a resource removes its expiry.

## Reviewing and removing

```
python3 src/main.py inventory expired [--env dev] [--service iam]
python3 src/main.py undeploy --expired [iam] [--env dev]              # dry-run
python3 src/main.py undeploy --expired --apply --confirm dev
```

`inventory list` also shows an `EXPIRES` column.

`undeploy --expired` selects the expired entries and then applies **all the normal undeploy
rules** ([undeploy.md](undeploy.md)): only `created`, never `adopted`, and **`keep` wins
over expiry**. An expired but kept resource is listed and skipped until you `unkeep` it.
With `--expired`, a service and `--env` are optional filters.

### Attachments and other dependents

A dependent, such as a role/policy attachment, **doesn't expire on its own while any
`created` resource it depends on hasn't expired**. Otherwise, extending a role's expiry
would still leave its attachment to be removed, silently breaking the role. When the role
or policy does expire, undeploy removes its attachments along with it.

## Automating it

Nothing runs on a schedule by itself. To automate the review, run
`inventory expired` (or `undeploy --expired` without `--apply`) from cron or CI and read
the output. An unattended `--apply` would need `--confirm`, which is deliberate friction:
do it only if you accept the deletions without review.
