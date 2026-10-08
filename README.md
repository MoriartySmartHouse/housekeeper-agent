# Housekeeper Apps for Home Assistant

**Housekeeper Agent** checks the health of a home once an hour (backups, repairs, updates, silent devices,
low batteries) and reports to the Housekeeper service run by whoever looks after the system. Inside the
home it appears as **Watson**, the house assistant.

Install: *Settings → Apps → App Store → ⋮ → Repositories* → add this repository's URL → install
**Housekeeper Agent**. See [housekeeper_agent/DOCS.md](housekeeper_agent/DOCS.md) for exactly what it reads
and what leaves your home, and [housekeeper_agent/CONTRACT.md](housekeeper_agent/CONTRACT.md) for every
Home Assistant interface it depends on.

## Security and releases
- Houses pull a prebuilt multi-arch image (`ghcr.io/moriartysmarthouse/housekeeper-agent`, amd64 + aarch64)
  built from this repository by [`.github/workflows/release.yaml`](.github/workflows/release.yaml) when a
  `v<version>` tag is pushed. Nothing is built on the house.
- The App runs under its own AppArmor profile ([`housekeeper_agent/apparmor.txt`](housekeeper_agent/apparmor.txt)).
- Images are not signed: the Home Assistant Supervisor does not verify signatures today.
- **Releasing:** bump `version` in `config.yaml` and `VERSION` in `agent.py`, move the CHANGELOG "Unreleased"
  entries under the new version, commit, push the tag `v<version>` **first**, wait for the *Release image*
  workflow to pass, then push `main`. Houses are offered the update only once `main` shows the new version,
  so the image always exists before anyone can install it.
