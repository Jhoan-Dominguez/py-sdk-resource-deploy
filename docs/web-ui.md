# Web UI

A local [Streamlit](https://streamlit.io) page over the same tool: see what each service can
deploy, what the inventory says is deployed, and run deploy, import, undeploy, keep/expire and
audit without typing the commands. It lives in `src/webui/` and is optional: the CLI doesn't
need it.

> **It has no login and acts with your AWS credentials**, including deleting IAM roles and
> policies. Run it on `127.0.0.1` only (that's what `make run-ui` does). Don't expose it on a
> network without putting authentication in front of it.

## Running it

```
pip install -r requirements-ui.txt        # requirements.txt + streamlit + pandas
make run-ui                               # exports .env, serves http://127.0.0.1:8501
```

Or, with the variables from `.env.template` already exported:

```
streamlit run src/webui/app.py --server.address 127.0.0.1
```

Before showing anything, the sidebar runs the same checks as the CLI: the required environment
variables are set, your credentials belong to `AWS_ACCOUNT_ID`, and the inventory table exists
or can be created. If one fails, it shows the error and stops.

## Pages

| Page | What it shows | What it runs |
|---|---|---|
| **Resumen** | Active / deleted / kept / expired counts, active resources per environment and type, per origin | `inventory init` (only if the table is missing) |
| **Catálogo** | Per service and environment: every resource the repo defines (from `definitions()`, without calling AWS), with its inventory status, or `no desplegado`. Warns about active entries no longer defined | — |
| **Inventario** | The inventory with service / environment / status filters, and the expired entries | On selected rows: `inventory keep` / `unkeep` / `expire` |
| **Desplegar** | — | `deploy <service> --env ... [--ttl ...]` |
| **Importar** | — | `inventory import <service> --env ... --origin ... [--reclassify] [--resource ...] [--ttl ...]` |
| **Eliminar** | Preview from the undeploy planner: what would be deleted, and what's skipped and why | `undeploy` by service + environment (+ resources), by deployment id, or `--expired` |
| **Auditoría** | — | `inventory audit` (report), then `--apply` (fixes the inventory only) |

## How writes work

The page never calls AWS write APIs itself. Every action runs the CLI (`deploy.cli.main`)
with the same arguments you would type, and shows the exact command and its output. So the
rules in [undeploy.md](undeploy.md), [inventory.md](inventory.md) and [audit.md](audit.md)
apply unchanged.

- **Dry-run first.** Deploy, import, undeploy and audit have a *Simular* button and an *Aplicar*
  button. *Aplicar* stays disabled until you've simulated **with exactly the same parameters**.
  After an apply, you have to simulate again.
- **Undeploy confirmation.** Before *Eliminar*, you have to type the affected environments
  (sorted, comma-separated, e.g. `dev,stg`). That text is passed as `--confirm`. As in the CLI,
  no environment is special.
- **Keep, unkeep and expire** only change the inventory. As in the CLI, they apply
  immediately. The page targets the resource id of each selected row, so each action changes
  only those entries, not every entry with the same name.

The preview on **Eliminar** and the CLI's `undeploy` share the same selection function
(`deploy.cli.undeploy_plan`), so the preview matches what the CLI will do. It's still read
before the click: if the inventory changes in between, the CLI's own plan (shown in the output)
is the one that counts.

## Adding a service

A service registered in `SERVICES` appears on every page automatically. To show up in
**Catálogo** (and in the resource picker on **Importar**), it must also implement
`definitions(environment)` (see [README.md](README.md#adding-a-service)).

## Limitations

See [notes.md](notes.md#known-limitations-and-pre-existing-issues): one command at a time,
inventory data up to 30 s old, and no login.
