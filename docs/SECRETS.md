# Secrets and what lives where

Nothing in this repository is a credential, and nothing here should ever
become one. This file says where the real secrets live so that stays true.

## What is in the repo

| File | Contains | Committed? |
|---|---|---|
| `.env.example` | placeholder addresses and names | yes |
| `.env` | **your** addresses, MAC, hostnames | **no** — gitignored |
| `src/**` | code, with documentation-range placeholder addresses | yes |
| `mirror/` | pulled copies of a live deployment | **no** — gitignored |

`.env` holds no passwords. It holds addresses, a MAC and hostnames — not
secret in the cryptographic sense, but they identify a specific person's
network and do not belong in a public repository.

## What is not in the repo, and where it lives

| Secret | Lives at | Mode |
|---|---|---|
| NUT user passwords | `/etc/nut/upsd.users` on the sentinel | `0640 root:nut` |
| NUT killpower password | `/etc/nut/killer.secret` | `0600 root:root` |
| UPS command credentials for the dashboard | `/etc/ups-dash/upscmd.json` | `0640` service user |
| ntfy token | `/etc/ups-dash/notify.json` | `0640` service user |

⚠ `/etc/ups-dash/upscmd.json` is the credential that lets a web page
**de-energise the UPS output**. It is created by `src/jetson/setup-upscmd-creds`,
which copies the existing NUT credential rather than inventing a second one.
Delete that file and both emergency-shutdown modes fail closed immediately.

## Reading a secret back

Read them from the machine, never from this repo:

```bash
sudo grep password /etc/nut/upsd.users        # on the sentinel
sudo ntfy token list <user>                   # if self-hosting ntfy
```

## If you mirror a live deployment

`scripts/pull-state.sh` copies live units, scripts and configs back into
`mirror/` so a dead SD card does not take the configuration with it. It
**scrubs credentials to `<secret>` before writing anything**, and `mirror/`
is gitignored regardless — the scrubbing is a second line of defence, not the
only one.

Verify after any change to that script:

```bash
grep -rn "password\|token" mirror/ | grep -v '<secret>'
```

That must return nothing.

## Before you publish a fork

```bash
# addresses, MACs, hostnames, UUIDs, serials
grep -rnE '([0-9]{1,3}\.){3}[0-9]{1,3}|([0-9a-f]{2}:){5}[0-9a-f]{2}' \
     --exclude-dir=.git --exclude=.env .

# anything token-shaped
grep -rnE 'tk_[A-Za-z0-9]{16,}|password"?\s*[:=]' --exclude-dir=.git .
```

Expect only documentation-range addresses (`10.0.0.x`), the placeholder MAC
`aa:bb:cc:dd:ee:ff`, and hardware constants (PCI addresses, register offsets).

⚠ **Git history is forever.** If a real value is ever committed, rewriting the
branch is not enough once it has been pushed — rotate the value instead.
